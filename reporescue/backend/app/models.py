from __future__ import annotations

"""SQLAlchemy 2.0 declarative models: ownership + investigations/jobs.

Phase 4 established profiles/projects; Phase 5A Steps 5-6 add persistent
investigations, attempts, execution runs, findings, patches, messages,
conversation sessions, and background job records. Column types use
portable SQLAlchemy generics (``Uuid``, ``JSON``) so the same models run
on PostgreSQL and on the isolated SQLite test database.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy import JSON, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all RepoRescue models."""


class Profile(Base):
    """One row per Supabase Auth user. ``id`` always equals ``auth.users.id``."""

    __tablename__ = "profiles"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(30), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    avatar_url: Mapped[str | None] = mapped_column(String(2000))
    github_username: Mapped[str | None] = mapped_column(String(100))
    github_user_id: Mapped[str | None] = mapped_column(String(50))
    github_avatar_url: Mapped[str | None] = mapped_column(String(2000))
    github_display_name: Mapped[str | None] = mapped_column(String(200))
    github_connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    projects: Mapped[list["Project"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan", passive_deletes=True
    )


class Project(Base):
    """A registered GitHub repository owned by one user.

    Canonical identity (``github_owner``/``repository_name``) is immutable;
    only ``display_name`` is editable. Uniqueness is scoped per owner.
    """

    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint(
            "owner_id", "github_owner", "repository_name",
            name="uq_projects_owner_repo",
        ),
        CheckConstraint(
            "visibility IN ('public')", name="ck_projects_visibility_public_only"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    github_repo_id: Mapped[int | None] = mapped_column(BigInteger)
    github_owner: Mapped[str] = mapped_column(String(200), nullable=False)
    repository_name: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    github_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    default_branch: Mapped[str | None] = mapped_column(String(200))
    visibility: Mapped[str] = mapped_column(String(20), nullable=False, default="public")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    owner: Mapped["Profile"] = relationship(back_populates="projects")


INVESTIGATION_STATUSES = ("queued", "running", "waiting", "completed", "failed", "cancelled")
INVESTIGATION_MODES = ("debug", "explain", "health")
VERIFICATION_STATUSES = ("unverified", "passed", "failed", "unavailable")
CONFIDENCES = ("confirmed", "hypothesis")
FINDING_KINDS = ("fact", "hypothesis")
RUN_KINDS = ("baseline", "verification")
PATCH_STATUSES = ("proposed", "validated", "applied", "rejected")
MESSAGE_ROLES = ("user", "assistant", "system")

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

# Allowed status transitions; terminal states have no exits. The worker
# and API validate every transition through transition_allowed().
STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"waiting", "completed", "failed", "cancelled"}),
    "waiting": frozenset({"running", "completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


def transition_allowed(from_status: str, to_status: str) -> bool:
    """True when a status change is legal (same-state writes allowed)."""
    if from_status == to_status:
        return True
    return to_status in STATUS_TRANSITIONS.get(from_status, frozenset())


class Investigation(Base):
    """One persisted investigation for any mode.

    The worker re-ingests from ``repo_url``/``commit_sha`` (pinned, so
    deterministic); profile/context snapshots are NOT stored wholesale —
    bounded evidence lives on findings and the final report blob.
    """

    __tablename__ = "investigations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'waiting', 'completed', 'failed', 'cancelled')",
            name="ck_investigations_status",
        ),
        CheckConstraint(
            "mode IN ('debug', 'explain', 'health', 'general')",
            name="ck_investigations_mode",
        ),
        CheckConstraint(
            "verification_status IN ('unverified', 'passed', 'failed', 'unavailable')",
            name="ck_investigations_verification_status",
        ),
        CheckConstraint(
            "progress_pct >= 0 AND progress_pct <= 100",
            name="ck_investigations_progress_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    repo_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    repo_name: Mapped[str] = mapped_column(String(400), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    ref: Mapped[str | None] = mapped_column(String(200))
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    selected_mode: Mapped[str | None] = mapped_column(String(20))
    routing: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    user_message: Mapped[str | None] = mapped_column(String(2000))
    max_attempts: Mapped[int] = mapped_column(nullable=False, default=3)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    stage: Mapped[str | None] = mapped_column(String(40))
    stages_completed: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    progress_pct: Mapped[int] = mapped_column(nullable=False, default=0)
    partial: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    verification_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unverified"
    )
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    limitations: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    final_report: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    timings: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    attempts: Mapped[list["InvestigationAttempt"]] = relationship(
        back_populates="investigation", cascade="all, delete-orphan", passive_deletes=True
    )
    runs: Mapped[list["ExecutionRun"]] = relationship(
        back_populates="investigation", cascade="all, delete-orphan", passive_deletes=True
    )
    findings: Mapped[list["Finding"]] = relationship(
        back_populates="investigation", cascade="all, delete-orphan", passive_deletes=True
    )
    patches: Mapped[list["Patch"]] = relationship(
        back_populates="investigation", cascade="all, delete-orphan", passive_deletes=True
    )
    messages: Mapped[list["InvestigationMessage"]] = relationship(
        back_populates="investigation", cascade="all, delete-orphan", passive_deletes=True
    )


class InvestigationAttempt(Base):
    """One diagnose-and-verify cycle within an investigation."""

    __tablename__ = "investigation_attempts"
    __table_args__ = (
        UniqueConstraint(
            "investigation_id", "attempt_no", name="uq_attempts_investigation_no"
        ),
        CheckConstraint(
            "confidence IN ('confirmed', 'hypothesis')",
            name="ck_attempts_confidence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_no: Mapped[int] = mapped_column(nullable=False)
    diagnosis: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(
        String(20), nullable=False, default="hypothesis"
    )
    patch: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    guard: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    verification: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    timings: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    investigation: Mapped["Investigation"] = relationship(back_populates="attempts")


class ExecutionRun(Base):
    """One bounded test-execution run (baseline or verification)."""

    __tablename__ = "execution_runs"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('baseline', 'verification')", name="ck_runs_kind"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("investigation_attempts.id", ondelete="SET NULL"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    backend: Mapped[str | None] = mapped_column(String(100))
    isolation: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str | None] = mapped_column(String(40))
    passed: Mapped[bool | None] = mapped_column()
    exit_code: Mapped[int | None] = mapped_column()
    output: Mapped[str | None] = mapped_column(Text)
    truncated: Mapped[bool] = mapped_column(default=False, nullable=False)
    failures: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    install_ok: Mapped[bool | None] = mapped_column()
    install_log: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    investigation: Mapped["Investigation"] = relationship(back_populates="runs")


class Finding(Base):
    """An observed fact or an AI hypothesis with bounded evidence."""

    __tablename__ = "findings"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('fact', 'hypothesis')", name="ck_findings_kind"
        ),
        CheckConstraint(
            "confidence IN ('confirmed', 'hypothesis')",
            name="ck_findings_confidence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    category: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    confidence: Mapped[str] = mapped_column(
        String(20), nullable=False, default="hypothesis"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    investigation: Mapped["Investigation"] = relationship(back_populates="findings")


class Patch(Base):
    """A proposed patch with its validation/application status."""

    __tablename__ = "patches"
    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed', 'validated', 'applied', 'rejected')",
            name="ck_patches_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("investigation_attempts.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="proposed"
    )
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    investigation: Mapped["Investigation"] = relationship(back_populates="patches")
    files: Mapped[list["PatchFile"]] = relationship(
        back_populates="patch", cascade="all, delete-orphan", passive_deletes=True
    )


class PatchFile(Base):
    """One file edit within a patch."""

    __tablename__ = "patch_files"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    patch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("patches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file: Mapped[str] = mapped_column(String(1000), nullable=False)
    before: Mapped[str | None] = mapped_column(Text)
    after: Mapped[str | None] = mapped_column(Text)

    patch: Mapped["Patch"] = relationship(back_populates="files")


class InvestigationMessage(Base):
    """One investigation-scoped conversation message."""

    __tablename__ = "investigation_messages"
    __table_args__ = (
        CheckConstraint(
            "role IN ('user', 'assistant', 'system')", name="ck_messages_role"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    investigation: Mapped["Investigation"] = relationship(back_populates="messages")


class ConversationSession(Base):
    """Persistence foundation for investigation-scoped AI conversation.

    Holds the canonical context snapshot a future conversation agent
    resumes from. No API routes yet; established here so later phases do
    not retrofit identity onto ad-hoc state.
    """

    __tablename__ = "conversation_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    context_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Job(Base):
    """One background execution claim for an investigation (1:1).

    Claiming is an atomic conditional UPDATE guarded by status; the
    worker holds a heartbeat-extended lease while running.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'waiting', 'completed', 'failed', 'cancelled')",
            name="ck_jobs_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(nullable=False, default=3)
    worker_id: Mapped[str | None] = mapped_column(String(100))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    not_before_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RepoSnapshot(Base):
    """One durable inspection snapshot owned by a user.

    Created alongside the in-memory ingestion record so job creation
    survives API restarts and browser refreshes. The worker still
    re-ingests from the pinned URL/SHA; this row only carries the
    validated coordinates, profile, and context the UI already saw.
    """

    __tablename__ = "repo_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    repo_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    repo_name: Mapped[str] = mapped_column(String(400), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    ref: Mapped[str | None] = mapped_column(String(200))
    profile: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    warnings: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    timings: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
