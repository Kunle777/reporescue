"""Authenticated project routes: register, list, rename, delete."""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import store
from .auth import AuthenticatedUser, get_current_user
from .github_ingest import IngestionError, normalize_github_url

router = APIRouter(prefix="/api/projects", tags=["projects"])


class CreateProjectRequest(BaseModel):
    github_url: str
    display_name: str | None = None
    default_branch: str | None = None


class RenameProjectRequest(BaseModel):
    display_name: str


def _public_project(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "display_name": row["display_name"],
        "github_owner": row["github_owner"],
        "repository_name": row["repository_name"],
        "github_url": row["github_url"],
        "default_branch": row.get("default_branch"),
        "visibility": row.get("visibility") or "public",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


@router.get("")
def list_projects(
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    return [_public_project(row) for row in store.list_projects(user.id)]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_project(
    body: CreateProjectRequest, user: AuthenticatedUser = Depends(get_current_user)
) -> dict[str, Any]:
    try:
        normalized = normalize_github_url((body.github_url or "").strip())
    except IngestionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    display_name = (body.display_name or "").strip() or (
        f"{normalized.owner}/{normalized.repo}"
    )
    default_branch = (body.default_branch or "").strip() or None
    now = store.utcnow_iso()
    row = {
        "id": uuid.uuid4().hex[:12],
        "user_id": user.id,
        "display_name": display_name,
        "github_owner": normalized.owner,
        "repository_name": normalized.repo,
        "github_url": normalized.url,
        "default_branch": default_branch,
        "visibility": "public",
        "created_at": now,
        "updated_at": now,
    }
    try:
        store.create_project(row)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Repository is already registered.",
        ) from exc
    return _public_project(row)


@router.patch("/{project_id}")
def rename_project(
    project_id: str,
    body: RenameProjectRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    display_name = (body.display_name or "").strip()
    if not display_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="display_name is required.",
        )
    row = store.update_project(project_id, user.id, {"display_name": display_name})
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found."
        )
    return _public_project(row)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: str, user: AuthenticatedUser = Depends(get_current_user)
) -> None:
    if not store.delete_project(project_id, user.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found."
        )
    return None
