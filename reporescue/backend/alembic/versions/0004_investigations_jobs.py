"""Persistent investigations and background jobs (Phase 5A Steps 5-6).

Additive: new tables only, no changes to existing tables or data.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_investigations_jobs"
down_revision = "0003_profiles_github_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "investigations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
        ),
        sa.Column("repo_url", sa.String(2000), nullable=False),
        sa.Column("repo_name", sa.String(400), nullable=False),
        sa.Column("commit_sha", sa.String(40), nullable=False),
        sa.Column("ref", sa.String(200)),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("user_message", sa.String(2000)),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default=sa.text("3")),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("stage", sa.String(40)),
        sa.Column("stages_completed", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("progress_pct", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("partial", postgresql.JSONB),
        sa.Column(
            "verification_status", sa.String(20), nullable=False,
            server_default=sa.text("'unverified'"),
        ),
        sa.Column("error", postgresql.JSONB),
        sa.Column("limitations", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("final_report", postgresql.JSONB),
        sa.Column("timings", postgresql.JSONB),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'waiting', 'completed', 'failed', 'cancelled')",
            name="ck_investigations_status",
        ),
        sa.CheckConstraint(
            "mode IN ('debug', 'explain', 'health')",
            name="ck_investigations_mode",
        ),
        sa.CheckConstraint(
            "verification_status IN ('unverified', 'passed', 'failed', 'unavailable')",
            name="ck_investigations_verification_status",
        ),
        sa.CheckConstraint(
            "progress_pct >= 0 AND progress_pct <= 100",
            name="ck_investigations_progress_range",
        ),
    )
    op.create_index("ix_investigations_owner_id", "investigations", ["owner_id"])
    op.create_index("ix_investigations_project_id", "investigations", ["project_id"])
    op.create_index(
        "ix_investigations_owner_status_updated",
        "investigations",
        ["owner_id", "status", "updated_at"],
    )

    op.create_table(
        "investigation_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "investigation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("investigations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_no", sa.Integer, nullable=False),
        sa.Column("diagnosis", sa.Text),
        sa.Column(
            "confidence", sa.String(20), nullable=False,
            server_default=sa.text("'hypothesis'"),
        ),
        sa.Column("patch", postgresql.JSONB),
        sa.Column("guard", postgresql.JSONB),
        sa.Column("verification", postgresql.JSONB),
        sa.Column("timings", postgresql.JSONB),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.UniqueConstraint(
            "investigation_id", "attempt_no", name="uq_attempts_investigation_no"
        ),
        sa.CheckConstraint(
            "confidence IN ('confirmed', 'hypothesis')",
            name="ck_attempts_confidence",
        ),
    )
    op.create_index(
        "ix_investigation_attempts_investigation_id",
        "investigation_attempts",
        ["investigation_id"],
    )

    op.create_table(
        "execution_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "investigation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("investigations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("investigation_attempts.id", ondelete="SET NULL"),
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("backend", sa.String(100)),
        sa.Column("isolation", sa.String(100)),
        sa.Column("status", sa.String(40)),
        sa.Column("passed", sa.Boolean),
        sa.Column("exit_code", sa.Integer),
        sa.Column("output", sa.Text),
        sa.Column("failures", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("install_ok", sa.Boolean),
        sa.Column("install_log", sa.Text),
        sa.Column("duration_ms", sa.Integer),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('baseline', 'verification')", name="ck_runs_kind"
        ),
    )
    op.create_index(
        "ix_execution_runs_investigation_id", "execution_runs", ["investigation_id"]
    )
    op.create_index(
        "ix_execution_runs_attempt_id", "execution_runs", ["attempt_id"]
    )

    op.create_table(
        "findings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "investigation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("investigations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("category", sa.String(100)),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("detail", sa.Text),
        sa.Column("evidence", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "confidence", sa.String(20), nullable=False,
            server_default=sa.text("'hypothesis'"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint("kind IN ('fact', 'hypothesis')", name="ck_findings_kind"),
        sa.CheckConstraint(
            "confidence IN ('confirmed', 'hypothesis')",
            name="ck_findings_confidence",
        ),
    )
    op.create_index("ix_findings_investigation_id", "findings", ["investigation_id"])

    op.create_table(
        "patches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "investigation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("investigations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("investigation_attempts.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "status", sa.String(20), nullable=False,
            server_default=sa.text("'proposed'"),
        ),
        sa.Column("reason", sa.Text),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'validated', 'applied', 'rejected')",
            name="ck_patches_status",
        ),
    )
    op.create_index("ix_patches_investigation_id", "patches", ["investigation_id"])
    op.create_index("ix_patches_attempt_id", "patches", ["attempt_id"])

    op.create_table(
        "patch_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "patch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("patches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("file", sa.String(1000), nullable=False),
        sa.Column("before", sa.Text),
        sa.Column("after", sa.Text),
    )
    op.create_index("ix_patch_files_patch_id", "patch_files", ["patch_id"])

    op.create_table(
        "investigation_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "investigation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("investigations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'system')", name="ck_messages_role"
        ),
    )
    op.create_index(
        "ix_investigation_messages_investigation_id",
        "investigation_messages",
        ["investigation_id"],
    )

    op.create_table(
        "conversation_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "investigation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("investigations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "context_snapshot", postgresql.JSONB, nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index(
        "ix_conversation_sessions_investigation_id",
        "conversation_sessions",
        ["investigation_id"],
    )
    op.create_index(
        "ix_conversation_sessions_owner_id",
        "conversation_sessions",
        ["owner_id"],
    )

    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "investigation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("investigations.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default=sa.text("3")),
        sa.Column("worker_id", sa.String(100)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("not_before_at", sa.DateTime(timezone=True)),
        sa.Column("error", postgresql.JSONB),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'waiting', 'completed', 'failed', 'cancelled')",
            name="ck_jobs_status",
        ),
    )
    op.create_index("ix_jobs_owner_id", "jobs", ["owner_id"])
    op.create_index("ix_jobs_investigation_id", "jobs", ["investigation_id"])
    op.create_index("ix_jobs_status_lease", "jobs", ["status", "lease_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_jobs_status_lease", table_name="jobs")
    op.drop_index("ix_jobs_investigation_id", table_name="jobs")
    op.drop_index("ix_jobs_owner_id", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_conversation_sessions_owner_id", table_name="conversation_sessions")
    op.drop_index(
        "ix_conversation_sessions_investigation_id", table_name="conversation_sessions"
    )
    op.drop_table("conversation_sessions")
    op.drop_index(
        "ix_investigation_messages_investigation_id",
        table_name="investigation_messages",
    )
    op.drop_table("investigation_messages")
    op.drop_index("ix_patch_files_patch_id", table_name="patch_files")
    op.drop_table("patch_files")
    op.drop_index("ix_patches_attempt_id", table_name="patches")
    op.drop_index("ix_patches_investigation_id", table_name="patches")
    op.drop_table("patches")
    op.drop_index("ix_findings_investigation_id", table_name="findings")
    op.drop_table("findings")
    op.drop_index("ix_execution_runs_attempt_id", table_name="execution_runs")
    op.drop_index(
        "ix_execution_runs_investigation_id", table_name="execution_runs"
    )
    op.drop_table("execution_runs")
    op.drop_index(
        "ix_investigation_attempts_investigation_id",
        table_name="investigation_attempts",
    )
    op.drop_table("investigation_attempts")
    op.drop_index(
        "ix_investigations_owner_status_updated", table_name="investigations"
    )
    op.drop_index("ix_investigations_project_id", table_name="investigations")
    op.drop_index("ix_investigations_owner_id", table_name="investigations")
    op.drop_table("investigations")
