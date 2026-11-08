from __future__ import annotations

"""Real GitHub repository ingestion: validate, clone, pin revision, clean up.

Phase 2 accepts public GitHub HTTPS URLs and clones them into unique
temporary workspaces. Cloning is inspection-only: nothing inside the
repository is executed by this module.

Security properties:
- Only the hosts in ALLOWED_HOSTS are accepted; anything else is rejected
  before any subprocess runs.
- URLs and refs are validated against strict allowlists so no caller input
  can inject extra git flags or shell syntax.
- All git invocations use argv lists (never a shell) with timeouts, a
  non-interactive environment, and secrets scrubbed from the environment.
"""

import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from . import config

ALLOWED_HOSTS = frozenset({"github.com", "www.github.com"})

CLONE_TIMEOUT_SECONDS = 120
GIT_COMMAND_TIMEOUT_SECONDS = 30
ERROR_TAIL_CHARS = 800
GITHUB_API_TIMEOUT_SECONDS = 5
GITHUB_API_URL = "https://api.github.com/repos"

# Extra path prefixes after owner/repo that are tolerated (and ignored) so
# pasted tree/blob/commit page URLs still resolve to the repository.
_TOLERATED_PREFIXES = frozenset(
    {"tree", "blob", "commit", "commits", "pull", "pulls", "issues", "releases", "actions", "compare"}
)

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_REF_CHAR_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

# Environment variable name fragments treated as secrets. Any variable whose
# name contains one of these is removed before git runs so cloned checkouts
# and their hooks can never observe backend credentials.
SECRET_ENV_HINTS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")


class IngestionError(Exception):
    """A repository ingestion failure with a machine-readable reason."""

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class NormalizedRepo:
    owner: str
    repo: str
    url: str


def normalize_github_url(raw_url: str) -> NormalizedRepo:
    """Validate a public GitHub repository URL and return its canonical form.

    Accepts ``https://github.com/<owner>/<repo>`` with an optional ``.git``
    suffix, trailing slash, or a tolerated page suffix such as
    ``/tree/<ref>``. Raises IngestionError for malformed URLs, unsupported
    hosts, and anything that could carry shell or git-flag injection.
    """
    if not isinstance(raw_url, str):
        raise IngestionError("Repository URL must be a string.", "invalid_url")
    text = raw_url.strip()
    if not text:
        raise IngestionError("Repository URL is empty.", "invalid_url")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise IngestionError("Repository URL contains control characters.", "invalid_url")
    if any(char.isspace() for char in text):
        raise IngestionError("Repository URL must not contain whitespace.", "invalid_url")

    try:
        parsed = urlparse(text)
    except ValueError as exc:
        raise IngestionError(f"Repository URL is malformed: {exc}.", "invalid_url") from exc

    if parsed.scheme != "https":
        raise IngestionError(
            "Only HTTPS GitHub repository URLs are supported.", "invalid_url"
        )
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise IngestionError(
            f"Unsupported repository host {host or parsed.netloc!r}; "
            "only github.com is supported.",
            "unsupported_host",
        )
    if "@" in parsed.netloc or parsed.port is not None:
        raise IngestionError(
            "Repository URLs with credentials or ports are not supported.", "invalid_url"
        )
    if parsed.username or parsed.password:
        raise IngestionError(
            "Repository URLs with credentials are not supported.", "invalid_url"
        )

    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) < 2:
        raise IngestionError(
            "Repository URL must include an owner and repository name.",
            "invalid_url",
        )
    if len(segments) > 2 and segments[2] not in _TOLERATED_PREFIXES:
        raise IngestionError(
            "Repository URL has an unexpected path; expected "
            "https://github.com/<owner>/<repo>.",
            "invalid_url",
        )
    owner, repo = segments[0], segments[1]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    for label, value in (("owner", owner), ("repository", repo)):
        if not value or value in (".", ".."):
            raise IngestionError(
                f"Repository URL has an invalid {label} name.", "invalid_url"
            )
        if len(value) > 100 or not _NAME_PATTERN.match(value):
            raise IngestionError(
                f"Repository URL has an invalid {label} name {value!r}.", "invalid_url"
            )
    return NormalizedRepo(
        owner=owner, repo=repo, url=f"https://github.com/{owner}/{repo}"
    )


def check_repo_visibility(owner: str, repo: str) -> str:
    """Best-effort visibility probe: public, private_or_missing, unknown.

    Unauthenticated: GitHub answers 404 for private repositories, so
    404 means "private or genuinely missing" and is rejected without
    cloning. Rate limits, network failures, and unexpected responses
    yield 'unknown' so ingestion falls back to the clone attempt
    instead of blocking on the API. Sends no credentials.
    """
    url = f"{GITHUB_API_URL}/{owner}/{repo}"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "RepoRescue-Beta"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=GITHUB_API_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "private_or_missing"
        return "unknown"
    except (urllib.error.URLError, OSError, ValueError):
        return "unknown"
    if not isinstance(payload, dict):
        return "unknown"
    if payload.get("private") is True:
        return "private_or_missing"
    if payload.get("private") is False:
        return "public"
    return "unknown"


def validate_ref(ref: str | None) -> str | None:
    """Validate an optional branch, tag, or commit ref.

    Returns the cleaned ref, or None when no ref was requested. The charset
    excludes whitespace and shell metacharacters, and values that git would
    interpret as flags or revisions tricks (``..``, ``@{``) are rejected.
    """
    if ref is None:
        return None
    if not isinstance(ref, str):
        raise IngestionError("Repository ref must be a string.", "invalid_ref")
    cleaned = ref.strip()
    if not cleaned:
        return None
    if (
        len(cleaned) > 200
        or not _REF_CHAR_PATTERN.match(cleaned)
        or ".." in cleaned
        or "@{" in cleaned
        or cleaned.startswith((".", "-", "/"))
        or cleaned.endswith(("/", ".", ".lock"))
        or "//" in cleaned
    ):
        raise IngestionError(f"Repository ref {ref!r} is invalid.", "invalid_ref")
    return cleaned


def scrub_env_for_subprocess() -> dict[str, str]:
    """Return an environment copy safe to hand to git.

    Secret-bearing variables are removed and git is forced non-interactive
    so a clone can never prompt for credentials and hang the request.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not any(hint in key.upper() for hint in SECRET_ENV_HINTS)
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run_git(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Run one git command with an argv list, no shell, and a timeout."""
    try:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=scrub_env_for_subprocess(),
        )
    except FileNotFoundError as exc:
        raise IngestionError(
            "git is not installed on the backend; cannot clone repositories.",
            "clone_failed",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise IngestionError(
            f"git {args[0] if args else ''} timed out after {timeout} seconds.",
            "timeout",
        ) from exc


def _classify_clone_failure(stderr: str) -> str:
    lowered = stderr.lower()
    if "repository not found" in lowered:
        return "not_found"
    if (
        "could not resolve host" in lowered
        or "connection refused" in lowered
        or "unable to access" in lowered
        or "connection timed out" in lowered
        or "operation timed out" in lowered
    ):
        return "network_error"
    return "clone_failed"


def _tree_size_bytes(root: Path, limit: int) -> int:
    """Return the checked-out tree size in bytes, stopping past ``limit``.

    Everything under ``root`` counts, including git metadata: all of it
    consumes disk. Symlinks are never followed. The walk exits early once
    the total exceeds ``limit`` so a huge tree cannot stall measurement.
    """
    total = 0
    stack = [root]
    while stack:
        try:
            entries = list(stack.pop().iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    stack.append(entry)
                else:
                    total += entry.stat().st_size
            except OSError:
                continue
            if total > limit:
                return total
    return total


def clone_and_pin(source: str, destination: Path, ref: str | None = None) -> str:
    """Clone ``source`` into ``destination`` and return the pinned commit SHA.

    ``source`` is normally a normalized GitHub URL; tests pass a local path
    to exercise clone behavior without network access. The optional ``ref``
    (branch, tag, or commit) is fetched and checked out detached, then the
    exact HEAD SHA is recorded. Raises IngestionError on any failure.
    File-writing commands run with ``core.longpaths=true`` so Windows
    checkouts with deep generated paths (e.g. ``.expo`` caches) succeed.
    """
    cleaned_ref = validate_ref(ref)
    if destination.exists():
        raise IngestionError(
            "Clone destination already exists; refusing to overwrite.",
            "clone_failed",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)

    result = _run_git(
        ["-c", "core.longpaths=true", "clone", "--depth", "1", "--no-tags",
         "--", source, str(destination)],
        CLONE_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[-ERROR_TAIL_CHARS:]
        raise IngestionError(
            f"Could not clone repository ({stderr or 'git clone failed'}).",
            _classify_clone_failure(result.stderr or ""),
        )

    if cleaned_ref:
        fetch = _run_git(
            ["-C", str(destination), "-c", "core.longpaths=true",
             "fetch", "--depth", "1", "--no-tags",
             "origin", cleaned_ref],
            GIT_COMMAND_TIMEOUT_SECONDS,
        )
        if fetch.returncode != 0:
            stderr = (fetch.stderr or "").strip()[-ERROR_TAIL_CHARS:]
            raise IngestionError(
                f"Could not fetch ref {cleaned_ref!r} "
                f"({stderr or 'unknown git error'}).",
                "invalid_ref",
            )
        checkout = _run_git(
            ["-C", str(destination), "-c", "core.longpaths=true",
             "checkout", "--detach", "--quiet", "FETCH_HEAD"],
            GIT_COMMAND_TIMEOUT_SECONDS,
        )
        if checkout.returncode != 0:
            stderr = (checkout.stderr or "").strip()[-ERROR_TAIL_CHARS:]
            raise IngestionError(
                f"Could not check out ref {cleaned_ref!r} "
                f"({stderr or 'unknown git error'}).",
                "invalid_ref",
            )

    sha_result = _run_git(
        ["-C", str(destination), "rev-parse", "HEAD"], GIT_COMMAND_TIMEOUT_SECONDS
    )
    sha = (sha_result.stdout or "").strip()
    if sha_result.returncode != 0 or not _SHA_PATTERN.match(sha):
        raise IngestionError(
            "Could not determine the cloned revision's commit SHA.",
            "clone_failed",
        )
    # Git offers no reliable in-flight size cap for clone: partial-clone
    # filters would omit blobs and silently yield an incomplete tree, so
    # the guard runs immediately after checkout instead. The clone timeout
    # remains the in-flight backstop; callers hold the destination inside
    # a temporary workspace that is removed on this error.
    try:
        max_bytes = config.get_max_clone_bytes()
    except config.ConfigError as exc:
        raise IngestionError(str(exc), "config_error") from exc
    if max_bytes > 0:
        size = _tree_size_bytes(destination, max_bytes)
        if size > max_bytes:
            raise IngestionError(
                f"Repository checkout ({size / 1_048_576:.1f} MB) exceeds "
                f"the supported ingestion limit "
                f"({max_bytes / 1_048_576:.1f} MB); refusing a partial clone.",
                "repo_too_large",
            )
    return sha


def _remove_readonly_retry(func, path: str, _exc_info) -> None:
    """shutil.rmtree handler: clear the read-only bit (git metadata on
    Windows) and retry once. Remaining failures are swallowed so cleanup
    never breaks the request."""
    try:
        os.chmod(path, 0o700)
    except OSError:
        return
    try:
        func(path)
    except OSError:
        pass


@contextlib.contextmanager
def temporary_workspace(prefix: str = "reporescue-repo-"):
    """Yield a unique temporary workspace, removing it afterwards.

    Cleanup is best-effort (a locked file must not break the request) and is
    confined to the directory this manager created.
    """
    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield path
    finally:
        remove_workspace(path)


def remove_workspace(path: str | Path) -> None:
    """Best-effort removal of a workspace directory created by this app.

    Shared by ``temporary_workspace`` and the orchestration ``RunScope``:
    clears the read-only bit git sets on metadata (Windows) and retries
    once; remaining failures are swallowed so cleanup never breaks the
    request.
    """
    shutil.rmtree(path, ignore_errors=False, onerror=_remove_readonly_retry)
