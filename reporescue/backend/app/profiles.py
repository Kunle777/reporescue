from __future__ import annotations

"""User profile helpers (Phase 4).

Profile rows are keyed by the verified Supabase Auth UUID and are created
lazily and idempotently: no manual step, no client-supplied owner ids.

Usernames are optional (OAuth users may have none) and unique when present.
The signup form collects a username attempt, which travels inside the
Supabase JWT's ``user_metadata``; this module validates it server-side and
the ``uq_profiles_username`` constraint enforces uniqueness. A taken or
invalid name never breaks profile creation: the profile stays valid without
one, and the owner can set it later through the profile endpoint.
"""

import re
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Profile

USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 30
USERNAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
RESERVED_USERNAMES = frozenset(
    {
        "admin",
        "administrator",
        "anonymous",
        "api",
        "null",
        "reporescue",
        "root",
        "support",
        "system",
        "undefined",
    }
)


class InvalidUsernameError(Exception):
    """The requested username breaks format, length, or reserved-name rules."""


class UsernameTakenError(Exception):
    """The requested username is already owned by another profile."""


class NoGitHubIdentityError(Exception):
    """The verified session carries no usable GitHub identity."""


class DifferentGitHubAccountError(Exception):
    """Another GitHub account is connected; replacing needs confirmation."""

    def __init__(self, stored_username: str | None) -> None:
        super().__init__("A different GitHub account is already connected.")
        self.stored_username = stored_username


def normalize_username(value: object) -> str | None:
    """Trim and lowercase a username attempt; None when not usable text."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    return cleaned or None


def validate_username(value: object) -> str | None:
    """Return the normalized username, or None when invalid or reserved.

    Server-side source of truth; the frontend mirrors these rules for
    instant feedback, but validation here plus the database unique
    constraint are the real enforcement.
    """
    cleaned = normalize_username(value)
    if cleaned is None:
        return None
    if not USERNAME_MIN_LENGTH <= len(cleaned) <= USERNAME_MAX_LENGTH:
        return None
    if USERNAME_PATTERN.match(cleaned) is None:
        return None
    if cleaned in RESERVED_USERNAMES:
        return None
    return cleaned


def username_attempt_from_claims(claims: Mapping[str, Any] | None) -> Any:
    """Extract the raw signup username attempt from verified JWT claims.

    Returns an untrusted value (or None); it must pass through
    :func:`validate_username` before storage. ``get_or_create_profile``
    does this internally.
    """
    if not isinstance(claims, dict):
        return None
    metadata = claims.get("user_metadata")
    if not isinstance(metadata, dict):
        return None
    return metadata.get("username")


def get_profile(db: Session, user_id: UUID) -> Profile | None:
    """Return the caller's profile, or None when it does not exist yet."""
    return db.get(Profile, user_id)


def get_or_create_profile(
    db: Session,
    *,
    user_id: UUID,
    display_name: str | None = None,
    avatar_url: str | None = None,
    username: str | None = None,
) -> Profile:
    """Return the caller's profile, creating it exactly once.

    ``username`` is an untrusted attempt: it is validated server-side, and
    when it is invalid or already taken the profile is still created without
    one instead of failing. Concurrent first requests race safely.
    """
    existing = db.get(Profile, user_id)
    if existing is not None:
        return existing
    validated = validate_username(username)
    profile = Profile(
        id=user_id, display_name=display_name, avatar_url=avatar_url,
        username=validated,
    )
    db.add(profile)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.get(Profile, user_id)
        if existing is not None:
            return existing
        if validated is None:
            raise
        profile = Profile(
            id=user_id, display_name=display_name, avatar_url=avatar_url,
            username=None,
        )
        db.add(profile)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.get(Profile, user_id)
            if existing is None:
                raise
            return existing
    db.refresh(profile)
    return profile


def _clean_text(value: object) -> str | None:
    """Trimmed string, or None for missing/blank/non-text values."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


def github_identity_from_claims(claims: Mapping[str, Any] | None) -> dict[str, str | None] | None:
    """Extract the verified GitHub identity from verified JWT claims.

    Returns ``{"github_user_id", "github_username", "github_avatar_url",
    "github_display_name"}`` or None when GitHub is not linked or the
    stable provider ID is missing (a session that cannot prove which
    GitHub account it is must not connect one).

    Field mapping follows Supabase's GitHub provider response
    (``user_name``/``provider_id``/``avatar_url``/``full_name``) with
    narrow documented fallbacks for optional fields. No live OAuth flow
    has been observed in this project yet; every lookup is type-checked
    so provider variations degrade to None instead of crashing.
    """
    if not isinstance(claims, dict):
        return None
    app_metadata = claims.get("app_metadata")
    if not isinstance(app_metadata, dict):
        return None
    providers = app_metadata.get("providers")
    linked = (isinstance(providers, list) and "github" in providers) or (
        app_metadata.get("provider") == "github"
    )
    if not linked:
        return None
    metadata = claims.get("user_metadata")
    if not isinstance(metadata, dict):
        return None
    provider_id = _clean_text(metadata.get("provider_id"))
    if provider_id is None:
        issuer = metadata.get("iss")
        subject = _clean_text(metadata.get("sub"))
        if (
            subject is not None
            and isinstance(issuer, str)
            and "github" in issuer.lower()
        ):
            provider_id = subject
    if provider_id is None:
        return None
    username = _clean_text(metadata.get("user_name")) or _clean_text(
        metadata.get("preferred_username")
    )
    display_name = _clean_text(metadata.get("full_name")) or _clean_text(
        metadata.get("name")
    )
    return {
        "github_user_id": provider_id,
        "github_username": username,
        "github_avatar_url": _clean_text(metadata.get("avatar_url")),
        "github_display_name": display_name,
    }


def sync_github_identity(
    db: Session, *, user_id: UUID, claims: Mapping[str, Any] | None, confirm: bool = False
) -> Profile:
    """Store the session's verified GitHub identity on the caller's profile.

    Creates the profile when needed. A renamed username for the same
    stable provider ID updates in place. Connecting a *different* GitHub
    account over an existing one requires ``confirm=True``, else
    :class:`DifferentGitHubAccountError`. Raises
    :class:`NoGitHubIdentityError` when the session proves no GitHub
    account. Never reads identity from request bodies.
    """
    identity = github_identity_from_claims(claims)
    if identity is None:
        raise NoGitHubIdentityError(
            "This session has no verified GitHub identity. "
            "Sign in with GitHub first."
        )
    profile = get_or_create_profile(db, user_id=user_id)
    stored_id = profile.github_user_id
    new_id = identity["github_user_id"]
    if stored_id is not None and stored_id != new_id and not confirm:
        raise DifferentGitHubAccountError(profile.github_username)
    profile.github_user_id = new_id
    profile.github_username = identity["github_username"]
    profile.github_avatar_url = identity["github_avatar_url"]
    profile.github_display_name = identity["github_display_name"]
    if stored_id != new_id or profile.github_connected_at is None:
        profile.github_connected_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(profile)
    return profile


def disconnect_github_identity(db: Session, *, user_id: UUID) -> Profile:
    """Clear the caller's GitHub link, keeping account and data intact.

    Only the five GitHub columns are nulled; username, projects, and
    everything else survive. Idempotent: already-disconnected profiles
    succeed without changes.
    """
    profile = get_or_create_profile(db, user_id=user_id)
    profile.github_user_id = None
    profile.github_username = None
    profile.github_avatar_url = None
    profile.github_display_name = None
    profile.github_connected_at = None
    db.commit()
    db.refresh(profile)
    return profile


def set_username(db: Session, *, user_id: UUID, username: str) -> Profile:
    """Set the caller's username, creating the profile if needed.

    Raises :class:`InvalidUsernameError` for bad names and
    :class:`UsernameTakenError` when another profile owns the name.
    Setting the current name again succeeds without changes.
    """
    normalized = validate_username(username)
    if normalized is None:
        raise InvalidUsernameError(
            "Usernames need 3-30 characters: a letter first, then lowercase "
            "letters, digits, or underscores."
        )
    profile = get_or_create_profile(db, user_id=user_id)
    if profile.username == normalized:
        return profile
    profile.username = normalized
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise UsernameTakenError("That username is taken.") from exc
    db.refresh(profile)
    return profile
