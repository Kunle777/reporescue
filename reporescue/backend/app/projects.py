from __future__ import annotations

"""Project CRUD with server-side ownership enforcement (Phase 4).

Every function takes ``owner_id`` from the verified JWT (never from client
input) and scopes all queries to it. Lookups combine id + owner in a single
query so cross-user access is indistinguishable from "not found".
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .github_ingest import normalize_github_url
from .models import Project


class DuplicateProjectError(Exception):
    """The owner already registered this GitHub repository."""


def create_project(
    db: Session,
    *,
    owner_id: UUID,
    github_url: str,
    display_name: str | None = None,
    default_branch: str | None = None,
) -> Project:
    """Register a public GitHub repository for ``owner_id``.

    Raises the URL validator's ``IngestionError`` for non-public-host URLs
    and :class:`DuplicateProjectError` when this owner already registered
    the same repository. The caller must ensure the owner's profile row
    exists first (foreign key).
    """
    normalized = normalize_github_url(github_url)
    name = (display_name or "").strip() or normalized.repo
    branch = (default_branch or "").strip() or None
    project = Project(
        owner_id=owner_id,
        github_owner=normalized.owner,
        repository_name=normalized.repo,
        display_name=name,
        github_url=normalized.url,
        default_branch=branch,
        visibility="public",
    )
    db.add(project)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise DuplicateProjectError(
            f"{normalized.owner}/{normalized.repo} is already registered."
        ) from exc
    db.refresh(project)
    return project


def list_projects(db: Session, *, owner_id: UUID) -> list[Project]:
    """Return the owner's projects, newest first."""
    return list(
        db.scalars(
            select(Project)
            .where(Project.owner_id == owner_id)
            .order_by(Project.created_at.desc(), Project.id.desc())
        ).all()
    )


def get_project(db: Session, *, owner_id: UUID, project_id: UUID) -> Project | None:
    """Return one owned project, or None (including other owners' rows)."""
    return db.scalars(
        select(Project).where(
            Project.id == project_id, Project.owner_id == owner_id
        )
    ).first()


def update_project_display_name(
    db: Session, *, owner_id: UUID, project_id: UUID, display_name: str
) -> Project | None:
    """Rename an owned project. Canonical repository identity never changes."""
    project = get_project(db, owner_id=owner_id, project_id=project_id)
    if project is None:
        return None
    project.display_name = display_name.strip()
    db.commit()
    db.refresh(project)
    return project


def delete_project(db: Session, *, owner_id: UUID, project_id: UUID) -> bool:
    """Delete an owned project. Returns False when there is nothing to delete."""
    project = get_project(db, owner_id=owner_id, project_id=project_id)
    if project is None:
        return False
    db.delete(project)
    db.commit()
    return True
