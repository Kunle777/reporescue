"""Authenticated profile routes: read, rename, GitHub link/unlink.

The GitHub identity always comes from the verified JWT's ``user_metadata``
(``user_name``, ``full_name``/``name``, ``avatar_url``); the request body
carries only the replace-confirmation flag, never the identity itself.
Access tokens do not carry the numeric GitHub id, so ``github_user_id``
stays null.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import store
from .auth import AuthenticatedUser, get_current_user

router = APIRouter(prefix="/api/profile", tags=["profile"])

USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,39}\Z")


class UpdateUsernameRequest(BaseModel):
    username: str


class SyncGithubRequest(BaseModel):
    confirm: bool = False


def _public_profile(row: dict[str, Any], user_id: str) -> dict[str, Any]:
    return {
        "id": user_id,
        "username": row.get("username"),
        "display_name": row.get("display_name"),
        "avatar_url": row.get("avatar_url"),
        "github_username": row.get("github_username"),
        "github_user_id": row.get("github_user_id"),
        "github_avatar_url": row.get("github_avatar_url"),
        "github_display_name": row.get("github_display_name"),
        "github_connected_at": row.get("github_connected_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _get_or_create(user_id: str) -> dict[str, Any]:
    row = store.get_profile(user_id)
    if row is not None:
        return row
    now = store.utcnow_iso()
    return store.save_profile({"user_id": user_id, "created_at": now, "updated_at": now})


def _github_identity_from_claims(claims: dict[str, Any]) -> dict[str, Any] | None:
    metadata = claims.get("user_metadata")
    if not isinstance(metadata, dict):
        return None
    username = metadata.get("user_name") or metadata.get("preferred_username")
    if not username or not str(username).strip():
        return None
    display_name = metadata.get("full_name") or metadata.get("name")
    avatar_url = metadata.get("avatar_url")
    return {
        "github_username": str(username).strip(),
        "github_user_id": None,
        "github_avatar_url": str(avatar_url).strip() if avatar_url else None,
        "github_display_name": str(display_name).strip() if display_name else None,
    }


@router.get("")
def get_profile(user: AuthenticatedUser = Depends(get_current_user)) -> dict[str, Any]:
    return _public_profile(_get_or_create(user.id), user.id)


@router.patch("")
def update_username(
    body: UpdateUsernameRequest, user: AuthenticatedUser = Depends(get_current_user)
) -> dict[str, Any]:
    username = (body.username or "").strip()
    if not USERNAME_PATTERN.fullmatch(username):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Username must be 1-39 characters: letters, digits, _ or -.",
        )
    if store.username_taken(username, user.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Username is taken."
        )
    row = store.save_profile(
        {"user_id": user.id, "username": username, "updated_at": store.utcnow_iso()}
    )
    return _public_profile(row, user.id)


@router.post("/github")
def sync_github_identity(
    body: SyncGithubRequest, user: AuthenticatedUser = Depends(get_current_user)
) -> dict[str, Any]:
    identity = _github_identity_from_claims(user.claims)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This session has no verified GitHub identity; "
            "sign in with GitHub first.",
        )
    row = _get_or_create(user.id)
    current = row.get("github_username")
    if current == identity["github_username"]:
        return _public_profile(row, user.id)
    if current and not body.confirm:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Already linked to {current}; confirm to replace.",
        )
    row = store.save_profile(
        {
            "user_id": user.id,
            **identity,
            "github_connected_at": store.utcnow_iso(),
            "updated_at": store.utcnow_iso(),
        }
    )
    return _public_profile(row, user.id)


@router.delete("/github")
def disconnect_github_identity(
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    _get_or_create(user.id)
    row = store.save_profile(
        {
            "user_id": user.id,
            "github_username": None,
            "github_user_id": None,
            "github_avatar_url": None,
            "github_display_name": None,
            "github_connected_at": None,
            "updated_at": store.utcnow_iso(),
        }
    )
    return _public_profile(row, user.id)
