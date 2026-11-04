"""Create profiles and projects tables (Phase 4 ownership foundation)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_profiles_projects"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("display_name", sa.String(200)),
        sa.Column("avatar_url", sa.String(2000)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_table(
        "projects",
        # No database-side UUID default: the ORM generates ids client-side.
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("github_repo_id", sa.BigInteger),
        sa.Column("github_owner", sa.String(200), nullable=False),
        sa.Column("repository_name", sa.String(200), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("github_url", sa.String(2000), nullable=False),
        sa.Column("default_branch", sa.String(200)),
        sa.Column(
            "visibility",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'public'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "owner_id", "github_owner", "repository_name",
            name="uq_projects_owner_repo",
        ),
        sa.CheckConstraint(
            "visibility IN ('public')", name="ck_projects_visibility_public_only"
        ),
    )
    op.create_index("ix_projects_owner_id", "projects", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_projects_owner_id", table_name="projects")
    op.drop_table("projects")
    op.drop_table("profiles")
