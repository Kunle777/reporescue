"""General-mode routing columns and durable inspection snapshots.

Revision ID: 0006_general_routing_snapshots
Revises: 0005_execution_output_truncated
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_general_routing_snapshots"
down_revision = "0005_execution_output_truncated"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("investigations") as batch_op:
        batch_op.drop_constraint("ck_investigations_mode", type_="check")
        batch_op.create_check_constraint(
            "ck_investigations_mode",
            "mode IN ('debug', 'explain', 'health', 'general')",
        )
        batch_op.add_column(
            sa.Column("selected_mode", sa.String(20), nullable=True)
        )
        batch_op.add_column(sa.Column("routing", sa.JSON(), nullable=True))
    op.create_table(
        "repo_snapshots",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("repo_url", sa.String(2000), nullable=False),
        sa.Column("repo_name", sa.String(400), nullable=False),
        sa.Column("commit_sha", sa.String(40), nullable=False),
        sa.Column("ref", sa.String(200), nullable=True),
        sa.Column("profile", sa.JSON(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("timings", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["profiles.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_repo_snapshots_owner_id", "repo_snapshots", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_repo_snapshots_owner_id", table_name="repo_snapshots")
    op.drop_table("repo_snapshots")
    with op.batch_alter_table("investigations") as batch_op:
        batch_op.drop_constraint("ck_investigations_mode", type_="check")
        batch_op.create_check_constraint(
            "ck_investigations_mode",
            "mode IN ('debug', 'explain', 'health')",
        )
        batch_op.drop_column("routing")
        batch_op.drop_column("selected_mode")
