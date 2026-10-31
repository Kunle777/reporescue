"""Tests for GitHub URL validation, safe cloning, SHA pinning, and cleanup.

All clone behavior is exercised against temporary LOCAL git repositories or
mocked subprocess calls. No test in this file touches the network.
"""

import os
import stat
import subprocess

import pytest

from app.github_ingest import (
    IngestionError,
    clone_and_pin,
    normalize_github_url,
    scrub_env_for_subprocess,
    temporary_workspace,
    validate_ref,
)

GIT_IDENTITY = ["-c", "user.email=test@example.com", "-c", "user.name=RepoRescue Test"]


def _git(args, cwd):
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    return result


def make_git_repo(path, files):
    """Create a local git repo with the given {relative_path: content} files."""
    path.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(["init", "-b", "main"], path)
    _git([*GIT_IDENTITY, "add", "."], path)
    _git([*GIT_IDENTITY, "commit", "-m", "initial"], path)
    return _git(["rev-parse", "HEAD"], path).stdout.strip()


DEMO_FILES = {
    "src/app.py": "VALUE = 1\n",
    "requirements.txt": "fastapi\n",
    "tests/test_app.py": "def test_value():\n    assert True\n",
}


# --- URL normalization ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://github.com/octocat/Hello-World", "https://github.com/octocat/Hello-World"),
        ("https://github.com/octocat/Hello-World.git", "https://github.com/octocat/Hello-World"),
        ("https://github.com/octocat/Hello-World/", "https://github.com/octocat/Hello-World"),
        ("https://github.com/octocat/Hello-World.git/", "https://github.com/octocat/Hello-World"),
        ("  https://github.com/octocat/Hello-World  ", "https://github.com/octocat/Hello-World"),
        ("https://www.github.com/octocat/Hello-World", "https://github.com/octocat/Hello-World"),
        ("https://github.com/octocat/Hello-World/tree/main", "https://github.com/octocat/Hello-World"),
        ("https://github.com/my-org/my.repo_2", "https://github.com/my-org/my.repo_2"),
    ],
)
def test_valid_github_urls_normalize(raw, expected):
    normalized = normalize_github_url(raw)
    assert normalized.url == expected
    assert normalized.owner == expected.rsplit("/", 2)[-2]
    assert normalized.repo == expected.rsplit("/", 2)[-1]


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not a url",
        "github.com/octocat/Hello-World",
        "http://github.com/octocat/Hello-World",
        "ftp://github.com/octocat/Hello-World",
        "https://github.com/octocat",
        "https://github.com/octocat/",
        "https://github.com//Hello-World",
        "https://github.com/octocat/Hello-World/unknown-extra/deep",
        "https://user:pass@github.com/octocat/Hello-World",
        "https://github.com:443/octocat/Hello-World",
        "https://github.com/octocat/Hello World",
        "https://github.com/octocat/Hello\nWorld",
        "https://github.com/octocat/Hello-World; rm -rf /",
        "https://github.com/octocat/Hello-World`id`",
        "https://github.com/octocat/$(whoami)",
        "https://github.com/octocat/Hello-World | cat /etc/passwd",
        "https://github.com/octocat/Hello-World&evil",
        "file:///tmp/some-repo",
        "https://github.com/../etc/passwd",
        "https://github.com/./x",
    ],
)
def test_malformed_urls_rejected(raw):
    with pytest.raises(IngestionError) as exc_info:
        normalize_github_url(raw)
    assert exc_info.value.reason == "invalid_url"


@pytest.mark.parametrize(
    "raw",
    [
        "https://gitlab.com/octocat/Hello-World",
        "https://bitbucket.org/octocat/Hello-World",
        "https://gist.github.com/octocat/abc123",
        "https://github.com.evil.com/octocat/Hello-World",
        "https://evilgithub.com/octocat/Hello-World",
        "https://github.io/octocat/Hello-World",
        "https://127.0.0.1/octocat/Hello-World",
        "https://raw.githubusercontent.com/octocat/Hello-World/main/README.md",
    ],
)
def test_unsupported_hosts_rejected(raw):
    with pytest.raises(IngestionError) as exc_info:
        normalize_github_url(raw)
    assert exc_info.value.reason == "unsupported_host"


def test_non_string_url_rejected():
    with pytest.raises(IngestionError) as exc_info:
        normalize_github_url(None)  # type: ignore[arg-type]
    assert exc_info.value.reason == "invalid_url"


# --- ref validation ---


@pytest.mark.parametrize("ref", [None, "", "   ", "main", "feature/x-1.2", "v1.0.0", "abc1234", "f" * 40])
def test_valid_refs_accepted(ref):
    assert validate_ref(ref) == (None if ref is None or not ref.strip() else ref.strip())


@pytest.mark.parametrize(
    "ref",
    [
        "../main",
        "main..feature",
        "-b",
        "/main",
        "main/",
        "main.lock",
        "br anch",
        "br\nanch",
        "a@{1}",
        "a~1",
        "a^1",
        "a:b",
        "a?b",
        "a*b",
        "a[b",
        "--upload-pack=evil",
        "; rm -rf /",
        "$(id)",
        "x" * 201,
    ],
)
def test_invalid_refs_rejected(ref):
    with pytest.raises(IngestionError) as exc_info:
        validate_ref(ref)
    assert exc_info.value.reason == "invalid_ref"


# --- clone behavior (local git repos, no network) ---


def test_clone_local_repo_pins_sha(tmp_path):
    source = tmp_path / "source"
    expected_sha = make_git_repo(source, DEMO_FILES)

    dest = tmp_path / "ws" / "repo"
    sha = clone_and_pin(str(source), dest)

    assert sha == expected_sha
    assert (dest / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert len(sha) == 40


def test_clone_branch_ref_pins_branch_head(tmp_path):
    source = tmp_path / "source"
    make_git_repo(source, DEMO_FILES)
    _git(["checkout", "-b", "feature"], source)
    (source / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git([*GIT_IDENTITY, "commit", "-am", "bump"], source)
    branch_sha = _git(["rev-parse", "HEAD"], source).stdout.strip()
    _git(["checkout", "main"], source)

    sha = clone_and_pin(str(source), tmp_path / "ws" / "repo", ref="feature")

    assert sha == branch_sha
    assert (tmp_path / "ws" / "repo" / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_clone_tag_ref_pins_tag(tmp_path):
    source = tmp_path / "source"
    expected_sha = make_git_repo(source, DEMO_FILES)
    _git([*GIT_IDENTITY, "tag", "v1.0.0"], source)

    sha = clone_and_pin(str(source), tmp_path / "ws" / "repo", ref="v1.0.0")

    assert sha == expected_sha


def test_clone_invalid_ref_reports_invalid_ref(tmp_path):
    source = tmp_path / "source"
    make_git_repo(source, DEMO_FILES)

    with pytest.raises(IngestionError) as exc_info:
        clone_and_pin(str(source), tmp_path / "ws" / "repo", ref="no-such-branch")
    assert exc_info.value.reason == "invalid_ref"


def test_clone_missing_source_reports_failure(tmp_path):
    with pytest.raises(IngestionError) as exc_info:
        clone_and_pin(str(tmp_path / "does-not-exist"), tmp_path / "ws" / "repo")
    assert exc_info.value.reason in ("clone_failed", "not_found")


def test_clone_refuses_existing_destination(tmp_path):
    source = tmp_path / "source"
    make_git_repo(source, DEMO_FILES)
    dest = tmp_path / "ws" / "repo"
    dest.mkdir(parents=True)

    with pytest.raises(IngestionError) as exc_info:
        clone_and_pin(str(source), dest)
    assert exc_info.value.reason == "clone_failed"


def test_clone_timeout_reports_timeout(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(IngestionError) as exc_info:
        clone_and_pin("https://github.com/octocat/Hello-World", tmp_path / "repo")
    assert exc_info.value.reason == "timeout"


def test_clone_missing_git_reports_failure(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(IngestionError) as exc_info:
        clone_and_pin("https://github.com/octocat/Hello-World", tmp_path / "repo")
    assert exc_info.value.reason == "clone_failed"


def test_clone_uses_argv_list_without_shell(tmp_path, monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        raise subprocess.TimeoutExpired(cmd=args, timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(IngestionError):
        clone_and_pin("https://github.com/octocat/Hello-World; evil", tmp_path / "repo")

    assert isinstance(seen["args"], list)
    assert seen["args"][0] == "git"
    assert "https://github.com/octocat/Hello-World; evil" in seen["args"]
    assert seen["kwargs"].get("shell") in (None, False)


# --- secret handling and cleanup ---


def test_subprocess_env_scrubs_secrets(monkeypatch):
    monkeypatch.setenv("META_API_KEY", "super-secret-key")
    monkeypatch.setenv("MY_AUTH_TOKEN", "super-secret-token")
    monkeypatch.setenv("DB_PASSWORD", "super-secret-password")
    monkeypatch.setenv("SAFE_SETTING", "keep-me")

    env = scrub_env_for_subprocess()

    blob = "\n".join(f"{key}={value}" for key, value in env.items())
    assert "super-secret" not in blob
    assert "META_API_KEY" not in env
    assert env["SAFE_SETTING"] == "keep-me"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_clone_passes_scrubbed_env_to_git(tmp_path, monkeypatch):
    source = tmp_path / "source"
    make_git_repo(source, DEMO_FILES)
    monkeypatch.setenv("META_API_KEY", "super-secret-key")
    real_run = subprocess.run
    seen_envs = []

    def spy_run(*args, **kwargs):
        seen_envs.append(kwargs.get("env", {}))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)
    clone_and_pin(str(source), tmp_path / "ws" / "repo")

    assert seen_envs
    for env in seen_envs:
        assert "META_API_KEY" not in env
        assert env.get("GIT_TERMINAL_PROMPT") == "0"


def test_temporary_workspace_is_removed():
    with temporary_workspace() as workspace:
        assert workspace.is_dir()
        (workspace / "repo").mkdir()
    assert not workspace.exists()


def test_temporary_workspace_is_removed_after_error(tmp_path):
    with pytest.raises(RuntimeError):
        with temporary_workspace() as workspace:
            assert workspace.is_dir()
            raise RuntimeError("boom")
    assert not workspace.exists()


def test_temporary_workspace_removes_readonly_files():
    with temporary_workspace() as workspace:
        locked_dir = workspace / "repo" / ".git" / "objects" / "ab"
        locked_dir.mkdir(parents=True)
        locked = locked_dir / "packfile"
        locked.write_text("pack-bytes", encoding="utf-8")
        os.chmod(locked, stat.S_IREAD)
    assert not workspace.exists()


def test_clone_into_workspace_leaves_no_residue(tmp_path):
    source = tmp_path / "source"
    make_git_repo(source, DEMO_FILES)
    with temporary_workspace() as workspace:
        dest = workspace / "repo"
        clone_and_pin(str(source), dest)
        assert (dest / "src" / "app.py").is_file()
    assert not workspace.exists()
