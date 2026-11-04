"""Acceptance tests for patch download (unified-diff export).

Covers owner scoping, latest-applied selection, exactly-once rendering
against the pinned snapshot, verified/unverified designation, size
limits, and temporary-workspace cleanup. No live network, Supabase,
Docker, or Modal is required: cloning is monkeypatched to a local
copy and git is only probed when available.
"""

import contextlib
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app import main as main_module
from app import patch_download as patch_download_module
from app.auth import AuthenticatedUser
from app.jobqueue import create_investigation
from app.models import Base, InvestigationAttempt, Patch, PatchFile, Profile
from app.patch_download import (
    PatchTooLargeError,
    PatchUnrenderableError,
    build_filename,
    is_verified,
    render_patch_text,
)

FAKE_SHA = "c" * 40
CALC_SOURCE = "def add(a, b):\n    return a - b\n"
CALC_BEFORE = "    return a - b\n"
CALC_AFTER = "    return a + b\n"


@pytest.fixture()
def engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path.as_posix()}/patch.db",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db(engine):
    with Session(engine) as session:
        yield session


@pytest.fixture()
def factory(engine):
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


@pytest.fixture()
def owner(db):
    profile = Profile(id=uuid.uuid4())
    db.add(profile)
    db.commit()
    return profile.id


@pytest.fixture()
def other_owner(db):
    profile = Profile(id=uuid.uuid4())
    db.add(profile)
    db.commit()
    return profile.id


def _user(owner_id):
    return AuthenticatedUser(id=owner_id, email=None, claims={})


def _repo_with_calc(tmp_path):
    root = tmp_path / "pinned"
    target = root / "pkg" / "calc.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(CALC_SOURCE, encoding="utf-8")
    return root


def _investigation(db, owner_id, sha=FAKE_SHA, verification_status="unverified"):
    investigation, _job = create_investigation(
        db,
        owner_id=owner_id,
        repo_url="https://github.com/acme/calc",
        repo_name="acme/calc",
        commit_sha=sha,
        mode="debug",
    )
    investigation.verification_status = verification_status
    db.commit()
    return investigation


def _applied_patch(db, investigation_id, before=CALC_BEFORE, after=CALC_AFTER,
                   rel="pkg/calc.py", status="applied"):
    patch = Patch(investigation_id=investigation_id, status=status, reason="Fix.")
    db.add(patch)
    db.flush()
    db.add(PatchFile(patch_id=patch.id, file=rel, before=before, after=after))
    db.commit()
    return patch


def _attempt(db, investigation_id, passed):
    attempt = InvestigationAttempt(
        investigation_id=investigation_id,
        attempt_no=1,
        verification={"passed": passed},
    )
    db.add(attempt)
    db.commit()
    return attempt


def _stub_clone(monkeypatch, source, sha=FAKE_SHA):
    def fake_clone(url, destination, ref=None):
        shutil.copytree(source, destination)
        return sha

    monkeypatch.setattr(patch_download_module, "clone_and_pin", fake_clone)


def test_render_valid_unified_diff(tmp_path):
    root = _repo_with_calc(tmp_path)
    edit = PatchFile(patch_id=uuid.uuid4(), file="pkg/calc.py",
                     before=CALC_BEFORE, after=CALC_AFTER)
    text = render_patch_text(
        repo_root=root,
        files=[edit],
        repo_url="https://github.com/acme/calc",
        commit_sha=FAKE_SHA,
        investigation_id=uuid.uuid4(),
        verified=True,
    )
    assert text.startswith("# RepoRescue patch export\n")
    assert f"# Commit: {FAKE_SHA}" in text
    assert "# Verification: PASSED" in text
    assert "diff --git a/pkg/calc.py b/pkg/calc.py" in text
    assert "--- a/pkg/calc.py" in text
    assert "+++ b/pkg/calc.py" in text
    assert "-    return a - b" in text
    assert "+    return a + b" in text


def test_render_unverified_header_warns(tmp_path):
    root = _repo_with_calc(tmp_path)
    edit = PatchFile(patch_id=uuid.uuid4(), file="pkg/calc.py",
                     before=CALC_BEFORE, after=CALC_AFTER)
    text = render_patch_text(
        repo_root=root,
        files=[edit],
        repo_url="https://github.com/acme/calc",
        commit_sha=FAKE_SHA,
        investigation_id=uuid.uuid4(),
        verified=False,
    )
    assert "# Verification: UNVERIFIED" in text
    assert "WARNING" in text
    assert "downloading is not applying" in text


def test_render_rejects_missing_before_block(tmp_path):
    root = _repo_with_calc(tmp_path)
    edit = PatchFile(patch_id=uuid.uuid4(), file="pkg/calc.py",
                     before="    return a * b\n", after="    return a + b\n")
    with pytest.raises(PatchUnrenderableError):
        render_patch_text(
            repo_root=root, files=[edit], repo_url="https://github.com/acme/calc",
            commit_sha=FAKE_SHA, investigation_id=uuid.uuid4(), verified=False,
        )


def test_render_rejects_ambiguous_before_block(tmp_path):
    root = tmp_path / "pinned"
    target = root / "dup.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x = 1\nx = 1\n", encoding="utf-8")
    edit = PatchFile(patch_id=uuid.uuid4(), file="dup.py",
                     before="x = 1\n", after="x = 2\n")
    with pytest.raises(PatchUnrenderableError):
        render_patch_text(
            repo_root=root, files=[edit], repo_url="https://github.com/acme/calc",
            commit_sha=FAKE_SHA, investigation_id=uuid.uuid4(), verified=False,
        )


def test_render_rejects_escape_path(tmp_path):
    root = _repo_with_calc(tmp_path)
    edit = PatchFile(patch_id=uuid.uuid4(), file="../evil.py",
                     before=CALC_BEFORE, after=CALC_AFTER)
    with pytest.raises(PatchUnrenderableError):
        render_patch_text(
            repo_root=root, files=[edit], repo_url="https://github.com/acme/calc",
            commit_sha=FAKE_SHA, investigation_id=uuid.uuid4(), verified=False,
        )


def test_render_rejects_empty_patch(tmp_path):
    root = _repo_with_calc(tmp_path)
    with pytest.raises(PatchUnrenderableError):
        render_patch_text(
            repo_root=root, files=[], repo_url="https://github.com/acme/calc",
            commit_sha=FAKE_SHA, investigation_id=uuid.uuid4(), verified=False,
        )


def test_render_enforces_size_limit(tmp_path, monkeypatch):
    root = _repo_with_calc(tmp_path)
    edit = PatchFile(patch_id=uuid.uuid4(), file="pkg/calc.py",
                     before=CALC_BEFORE, after=CALC_AFTER)
    monkeypatch.setattr(patch_download_module, "MAX_PATCH_BYTES", 100)
    with pytest.raises(PatchTooLargeError):
        render_patch_text(
            repo_root=root, files=[edit], repo_url="https://github.com/acme/calc",
            commit_sha=FAKE_SHA, investigation_id=uuid.uuid4(), verified=True,
        )


def test_verified_requires_status_and_attempt(db, owner):
    yes = _investigation(db, owner, verification_status="passed")
    _attempt(db, yes.id, True)
    assert is_verified(db, yes) is True
    failed = _investigation(db, owner, verification_status="failed")
    _attempt(db, failed.id, False)
    assert is_verified(db, failed) is False
    mismatch = _investigation(db, owner, verification_status="passed")
    _attempt(db, mismatch.id, False)
    assert is_verified(db, mismatch) is False


def test_filename_marks_verification_status():
    ident = uuid.uuid4()
    verified = build_filename("acme/calc", FAKE_SHA, ident, True)
    assert verified.endswith("-verified.patch")
    assert FAKE_SHA[:7] in verified
    assert str(ident)[:8] in verified
    unverified = build_filename("acme/calc", FAKE_SHA, ident, False)
    assert unverified.endswith("-unverified.patch")
    assert unverified.isascii()


def test_route_returns_attachment_for_owner(db, owner, tmp_path, monkeypatch):
    investigation = _investigation(db, owner, verification_status="passed")
    _applied_patch(db, investigation.id)
    _attempt(db, investigation.id, True)
    _stub_clone(monkeypatch, _repo_with_calc(tmp_path))
    response = main_module.download_investigation_patch(
        investigation.id, _user(owner), db
    )
    assert response.media_type == "text/x-patch"
    assert response.headers["X-Patch-Verified"] == "true"
    assert response.headers["X-Patch-Commit"] == FAKE_SHA
    assert "verified.patch" in response.headers["Content-Disposition"]
    body = response.body.decode("utf-8")
    assert "diff --git a/pkg/calc.py b/pkg/calc.py" in body


def test_route_cross_user_is_404(db, owner, other_owner):
    investigation = _investigation(db, owner)
    with pytest.raises(Exception) as excinfo:
        main_module.download_investigation_patch(
            investigation.id, _user(other_owner), db
        )
    assert excinfo.value.status_code == 404


def test_route_missing_patch_is_404(db, owner):
    investigation = _investigation(db, owner)
    with pytest.raises(Exception) as excinfo:
        main_module.download_investigation_patch(
            investigation.id, _user(owner), db
        )
    assert excinfo.value.status_code == 404


def test_route_unrenderable_is_422(db, owner, tmp_path, monkeypatch):
    investigation = _investigation(db, owner)
    _applied_patch(db, investigation.id, before="    return a * b\n")
    _stub_clone(monkeypatch, _repo_with_calc(tmp_path))
    with pytest.raises(Exception) as excinfo:
        main_module.download_investigation_patch(
            investigation.id, _user(owner), db
        )
    assert excinfo.value.status_code == 422


def test_route_oversized_is_413(db, owner, tmp_path, monkeypatch):
    investigation = _investigation(db, owner)
    _applied_patch(db, investigation.id)
    _stub_clone(monkeypatch, _repo_with_calc(tmp_path))
    monkeypatch.setattr(patch_download_module, "MAX_PATCH_BYTES", 100)
    with pytest.raises(Exception) as excinfo:
        main_module.download_investigation_patch(
            investigation.id, _user(owner), db
        )
    assert excinfo.value.status_code == 413


def test_route_snapshot_mismatch_is_422(db, owner, tmp_path, monkeypatch):
    investigation = _investigation(db, owner)
    _applied_patch(db, investigation.id)
    _stub_clone(monkeypatch, _repo_with_calc(tmp_path), sha="d" * 40)
    with pytest.raises(Exception) as excinfo:
        main_module.download_investigation_patch(
            investigation.id, _user(owner), db
        )
    assert excinfo.value.status_code == 422


def test_workspace_cleaned_on_success_and_error(db, owner, tmp_path, monkeypatch):
    seen: list[Path] = []
    real_workspace = patch_download_module.temporary_workspace

    @contextlib.contextmanager
    def spy(prefix="reporescue-patch-"):
        with real_workspace(prefix=prefix) as workspace:
            seen.append(Path(workspace))
            yield workspace

    monkeypatch.setattr(patch_download_module, "temporary_workspace", spy)
    good = _investigation(db, owner)
    _applied_patch(db, good.id)
    _stub_clone(monkeypatch, _repo_with_calc(tmp_path))
    main_module.download_investigation_patch(good.id, _user(owner), db)
    assert seen and not seen[-1].exists()
    bad = _investigation(db, owner)
    _applied_patch(db, bad.id, before="    return a * b\n")
    with pytest.raises(Exception):
        main_module.download_investigation_patch(bad.id, _user(owner), db)
    assert not seen[-1].exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git CLI is unavailable")
def test_rendered_patch_applies_with_git(tmp_path):
    repo = tmp_path / "apply"
    target = repo / "pkg" / "calc.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(CALC_SOURCE, encoding="utf-8")
    edit = PatchFile(patch_id=uuid.uuid4(), file="pkg/calc.py",
                     before=CALC_BEFORE, after=CALC_AFTER)
    text = render_patch_text(
        repo_root=repo,
        files=[edit],
        repo_url="https://github.com/acme/calc",
        commit_sha=FAKE_SHA,
        investigation_id=uuid.uuid4(),
        verified=True,
    )
    patch_file = tmp_path / "export.patch"
    patch_file.write_text(text, encoding="utf-8")
    completed = subprocess.run(
        ["git", "apply", "--check", str(patch_file)],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
