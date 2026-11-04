"""Username validation, persistence, conflicts, and profile routes.

Uses throwaway SQLite databases plus direct route calls; no live database
or Supabase Auth is required.
"""

import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import main as main_module
from app.auth import AuthenticatedUser
from app.models import Base, Profile
from app.profiles import (
    InvalidUsernameError,
    UsernameTakenError,
    get_or_create_profile,
    normalize_username,
    set_username,
    username_attempt_from_claims,
    validate_username,
)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path.as_posix()}/u.db")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _user(**claims):
    return AuthenticatedUser(id=uuid.uuid4(), email="tester@example.com", claims=claims)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("alice", "alice"),
        ("Alice_01", "alice_01"),
        ("  bob  ", "bob"),
        ("a12", "a12"),
        ("x" * 30, "x" * 30),
    ],
)
def test_validate_username_accepts(raw, expected):
    assert validate_username(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "  ", "ab", "x" * 31, "1abc", "a-b", "a b", "a.b", "admin", "ROOT", "Admin",
     "support", "reporescue", "null", "undefined", None, 123, ["alice"]],
)
def test_validate_username_rejects(raw):
    assert validate_username(raw) is None


def test_normalize_username_non_text():
    assert normalize_username(None) is None
    assert normalize_username(42) is None
    assert normalize_username("") is None


def test_username_attempt_from_claims():
    assert (
        username_attempt_from_claims({"user_metadata": {"username": "Alice"}})
        == "Alice"
    )
    assert username_attempt_from_claims({}) is None
    assert username_attempt_from_claims(None) is None
    assert username_attempt_from_claims({"user_metadata": "nope"}) is None
    assert username_attempt_from_claims({"user_metadata": {}}) is None


def test_create_profile_persists_valid_username(db):
    profile = get_or_create_profile(db, user_id=uuid.uuid4(), username="Alice_1")
    assert profile.username == "alice_1"


def test_create_profile_invalid_username_stays_valid_without_one(db):
    profile = get_or_create_profile(db, user_id=uuid.uuid4(), username="admin")
    assert profile.id is not None
    assert profile.username is None


def test_create_profile_username_conflict_stays_valid_without_one(db):
    get_or_create_profile(db, user_id=uuid.uuid4(), username="alice")
    second = get_or_create_profile(db, user_id=uuid.uuid4(), username="alice")
    assert second.id is not None
    assert second.username is None


def test_create_profile_without_username_for_oauth(db):
    profile = get_or_create_profile(db, user_id=uuid.uuid4())
    assert profile.id is not None
    assert profile.username is None


def test_username_unique_constraint_rejects_duplicates(db):
    db.add(Profile(id=uuid.uuid4(), username="alice"))
    db.commit()
    db.add(Profile(id=uuid.uuid4(), username="alice"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_username_null_allowed_many_times(db):
    db.add(Profile(id=uuid.uuid4(), username=None))
    db.add(Profile(id=uuid.uuid4()))
    db.commit()


def test_set_username_creates_and_sets(db):
    user_id = uuid.uuid4()
    profile = set_username(db, user_id=user_id, username="Bob")
    assert profile.username == "bob"
    assert db.get(Profile, user_id).username == "bob"


def test_set_username_idempotent(db):
    user_id = uuid.uuid4()
    set_username(db, user_id=user_id, username="bob")
    again = set_username(db, user_id=user_id, username="  BOB ")
    assert again.username == "bob"


def test_set_username_taken(db):
    set_username(db, user_id=uuid.uuid4(), username="alice")
    other_id = uuid.uuid4()
    with pytest.raises(UsernameTakenError):
        set_username(db, user_id=other_id, username="alice")
    assert db.get(Profile, other_id).username is None


def test_set_username_invalid(db):
    with pytest.raises(InvalidUsernameError):
        set_username(db, user_id=uuid.uuid4(), username="x")


def test_get_profile_route_404_then_200(db):
    user = _user()
    with pytest.raises(HTTPException) as exc_info:
        main_module.get_profile_route(user, db)
    assert exc_info.value.status_code == 404
    set_username(db, user_id=user.id, username="alice")
    fetched = main_module.get_profile_route(user, db)
    assert fetched.username == "alice"


def test_patch_profile_route_sets_changes_conflicts(db):
    user = _user()
    created = main_module.update_profile_route(
        main_module.ProfileUpdate(username="alice"), user, db
    )
    assert created.username == "alice"

    renamed = main_module.update_profile_route(
        main_module.ProfileUpdate(username="alice2"), user, db
    )
    assert renamed.username == "alice2"

    other = _user()
    with pytest.raises(HTTPException) as exc_info:
        main_module.update_profile_route(
            main_module.ProfileUpdate(username="alice2"), other, db
        )
    assert exc_info.value.status_code == 409
    assert other.id != user.id


def test_patch_profile_route_rejects_invalid_username(db):
    with pytest.raises(HTTPException) as exc_info:
        main_module.update_profile_route(
            main_module.ProfileUpdate(username="ab"), _user(), db
        )
    assert exc_info.value.status_code == 422
    with pytest.raises(ValidationError):
        main_module.ProfileUpdate.model_validate({})


def test_create_project_persists_signup_username(db):
    user = _user(user_metadata={"username": "Alice"})
    project = main_module.create_project_route(
        main_module.ProjectCreate(github_url="https://github.com/acme/calc"),
        user,
        db,
    )
    assert project.id is not None
    assert db.get(Profile, user.id).username == "alice"


def test_create_project_survives_username_conflict(db):
    first = _user(user_metadata={"username": "alice"})
    main_module.create_project_route(
        main_module.ProjectCreate(github_url="https://github.com/acme/one"),
        first,
        db,
    )
    second = _user(user_metadata={"username": "alice"})
    project = main_module.create_project_route(
        main_module.ProjectCreate(github_url="https://github.com/acme/two"),
        second,
        db,
    )
    assert project.id is not None
    assert db.get(Profile, second.id).username is None
