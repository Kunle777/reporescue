"""Verified GitHub identity sync: parser, sync/disconnect, routes.

Uses throwaway SQLite databases plus direct route calls with fabricated
*verified-claims-shaped* dicts; no live Supabase Auth is required. The
claims fixtures mirror Supabase's documented GitHub provider response
(user_name/provider_id/avatar_url/full_name); no live OAuth flow has been
observed in this project yet.
"""

import uuid
from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app import main as main_module
from app.auth import AuthenticatedUser
from app.models import Base, Profile, Project
from app.profiles import (
    DifferentGitHubAccountError,
    NoGitHubIdentityError,
    disconnect_github_identity,
    get_or_create_profile,
    github_identity_from_claims,
    set_username,
    sync_github_identity,
)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path.as_posix()}/g.db")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def github_claims(**user_metadata):
    base = {
        "user_name": "Kunle777",
        "provider_id": "12345678",
        "avatar_url": "https://avatars.githubusercontent.com/u/12345678",
        "full_name": "Abdulrazaq Olanipekun",
        "email": "dev@example.com",
        "iss": "https://api.github.com",
        "sub": "12345678",
    }
    base.update(user_metadata)
    return {
        "sub": str(uuid.uuid4()),
        "app_metadata": {"provider": "github", "providers": ["github"]},
        "user_metadata": base,
    }


def email_claims():
    return {
        "sub": str(uuid.uuid4()),
        "app_metadata": {"provider": "email", "providers": ["email"]},
        "user_metadata": {"username": "ada_debugs"},
    }


def linked_claims(**user_metadata):
    """Email signup that later linked GitHub: merged metadata, both providers."""
    claims = github_claims(**user_metadata)
    claims["app_metadata"] = {"provider": "email", "providers": ["email", "github"]}
    claims["user_metadata"]["username"] = "ada_debugs"
    return claims


def _user(claims):
    return AuthenticatedUser(
        id=uuid.uuid4(), email="dev@example.com", claims=claims
    )


# --- parser ---


def test_parser_complete_identity():
    assert github_identity_from_claims(github_claims()) == {
        "github_user_id": "12345678",
        "github_username": "Kunle777",
        "github_avatar_url": "https://avatars.githubusercontent.com/u/12345678",
        "github_display_name": "Abdulrazaq Olanipekun",
    }


def test_parser_missing_optionals_ok():
    claims = github_claims()
    del claims["user_metadata"]["avatar_url"]
    del claims["user_metadata"]["full_name"]
    identity = github_identity_from_claims(claims)
    assert identity is not None
    assert identity["github_user_id"] == "12345678"
    assert identity["github_username"] == "Kunle777"
    assert identity["github_avatar_url"] is None
    assert identity["github_display_name"] is None


def test_parser_fallback_usernames():
    assert (
        github_identity_from_claims(
            github_claims(user_name="  ", preferred_username="fallback_name")
        )["github_username"]
        == "fallback_name"
    )
    assert (
        github_identity_from_claims(
            github_claims(full_name="  ", name="Plain Name")
        )["github_display_name"]
        == "Plain Name"
    )


def test_parser_sub_fallback_needs_github_issuer():
    no_pid = github_claims()
    del no_pid["user_metadata"]["provider_id"]
    assert github_identity_from_claims(no_pid)["github_user_id"] == "12345678"

    other_iss = github_claims()
    del other_iss["user_metadata"]["provider_id"]
    other_iss["user_metadata"]["iss"] = "https://accounts.google.com"
    assert github_identity_from_claims(other_iss) is None


def test_parser_no_github_linked():
    assert github_identity_from_claims(email_claims()) is None
    assert github_identity_from_claims({}) is None
    assert github_identity_from_claims(None) is None
    assert github_identity_from_claims({"app_metadata": {}, "user_metadata": {}}) is None


def test_parser_malformed_metadata_safe():
    assert (
        github_identity_from_claims(
            {"app_metadata": {"providers": ["github"]}, "user_metadata": "nope"}
        )
        is None
    )
    assert (
        github_identity_from_claims(
            {
                "app_metadata": {"providers": ["github"]},
                "user_metadata": {"provider_id": 12345678, "user_name": ["x"]},
            }
        )
        is None
    )
    assert (
        github_identity_from_claims({"app_metadata": "nope", "user_metadata": {}})
        is None
    )


def test_parser_linked_email_account():
    identity = github_identity_from_claims(linked_claims())
    assert identity is not None
    assert identity["github_username"] == "Kunle777"


# --- sync ---


def test_sync_connects_on_signup(db):
    profile = sync_github_identity(db, user_id=uuid.uuid4(), claims=github_claims())
    assert profile.github_user_id == "12345678"
    assert profile.github_username == "Kunle777"
    assert profile.github_display_name == "Abdulrazaq Olanipekun"
    assert profile.github_avatar_url is not None
    assert isinstance(profile.github_connected_at, datetime)
    assert profile.username is None


def test_sync_existing_email_account(db):
    user_id = uuid.uuid4()
    get_or_create_profile(db, user_id=user_id, username="ada_debugs")
    profile = sync_github_identity(db, user_id=user_id, claims=linked_claims())
    assert profile.username == "ada_debugs"
    assert profile.github_username == "Kunle777"


def test_sync_rename_same_id_updates(db):
    user_id = uuid.uuid4()
    first = sync_github_identity(db, user_id=user_id, claims=github_claims())
    connected_at = first.github_connected_at
    renamed = sync_github_identity(
        db, user_id=user_id, claims=github_claims(user_name="Kunle999")
    )
    assert renamed.github_user_id == "12345678"
    assert renamed.github_username == "Kunle999"
    assert renamed.github_connected_at == connected_at


def test_sync_different_account_needs_confirmation(db):
    user_id = uuid.uuid4()
    sync_github_identity(db, user_id=user_id, claims=github_claims())
    other = github_claims(user_name="someone", provider_id="999", sub="999")
    with pytest.raises(DifferentGitHubAccountError) as exc_info:
        sync_github_identity(db, user_id=user_id, claims=other)
    assert exc_info.value.stored_username == "Kunle777"
    assert db.get(Profile, user_id).github_username == "Kunle777"

    replaced = sync_github_identity(
        db, user_id=user_id, claims=other, confirm=True
    )
    assert replaced.github_user_id == "999"
    assert replaced.github_username == "someone"


def test_sync_without_identity_errors(db):
    with pytest.raises(NoGitHubIdentityError):
        sync_github_identity(db, user_id=uuid.uuid4(), claims=email_claims())


def test_sync_without_identity_on_unlinked_claims(db):
    with pytest.raises(NoGitHubIdentityError):
        sync_github_identity(db, user_id=uuid.uuid4(), claims=None)


# --- disconnect ---


def test_disconnect_clears_only_github(db):
    user_id = uuid.uuid4()
    set_username(db, user_id=user_id, username="ada_debugs")
    sync_github_identity(db, user_id=user_id, claims=github_claims())
    db.add(
        Project(
            id=uuid.uuid4(),
            owner_id=user_id,
            github_owner="acme",
            repository_name="calc",
            display_name="calc",
            github_url="https://github.com/acme/calc",
        )
    )
    db.commit()

    cleared = disconnect_github_identity(db, user_id=user_id)
    assert cleared.github_user_id is None
    assert cleared.github_username is None
    assert cleared.github_avatar_url is None
    assert cleared.github_display_name is None
    assert cleared.github_connected_at is None
    assert cleared.username == "ada_debugs"
    assert db.get(Profile, user_id) is not None


def test_disconnect_idempotent(db):
    user_id = uuid.uuid4()
    first = disconnect_github_identity(db, user_id=user_id)
    assert first.github_user_id is None
    second = disconnect_github_identity(db, user_id=user_id)
    assert second.id == user_id


# --- routes ---


def test_sync_route_200_409_400(db):
    user = _user(github_claims())
    created = main_module.sync_github_route(
        main_module.GitHubSyncRequest(), user, db
    )
    assert created.github_username == "Kunle777"

    other_user = AuthenticatedUser(
        id=user.id,
        email="dev@example.com",
        claims=github_claims(user_name="someone", provider_id="999", sub="999"),
    )
    with pytest.raises(HTTPException) as exc_info:
        main_module.sync_github_route(main_module.GitHubSyncRequest(), other_user, db)
    assert exc_info.value.status_code == 409
    assert "@Kunle777" in exc_info.value.detail

    replaced = main_module.sync_github_route(
        main_module.GitHubSyncRequest(confirm=True), other_user, db
    )
    assert replaced.github_username == "someone"

    email_user = _user(email_claims())
    with pytest.raises(HTTPException) as exc_info:
        main_module.sync_github_route(
            main_module.GitHubSyncRequest(), email_user, db
        )
    assert exc_info.value.status_code == 400


def test_disconnect_route(db):
    user = _user(github_claims())
    main_module.sync_github_route(main_module.GitHubSyncRequest(), user, db)
    cleared = main_module.disconnect_github_route(user, db)
    assert cleared.github_username is None
    assert cleared.id == user.id


def test_patch_ignores_spoofed_github_username(db):
    """ProfileUpdate carries username only: extra github keys never land."""
    user = _user(email_claims())
    body = main_module.ProfileUpdate.model_validate(
        {"username": "ada_debugs", "github_username": "spoofed"}
    )
    assert not hasattr(body, "github_username")
    profile = main_module.update_profile_route(body, user, db)
    assert profile.username == "ada_debugs"
    assert profile.github_username is None
    assert profile.github_user_id is None


def test_profile_response_carries_github_fields(db):
    user = _user(github_claims())
    profile = main_module.sync_github_route(
        main_module.GitHubSyncRequest(), user, db
    )
    response = main_module.ProfileResponse.model_validate(profile)
    assert response.github_username == "Kunle777"
    assert response.github_user_id == "12345678"
    assert response.github_connected_at is not None
