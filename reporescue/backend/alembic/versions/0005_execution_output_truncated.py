"""Persist execution output truncation metadata.

Revision ID: 0005_execution_output_truncated
Revises: 0004_investigations_jobs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_execution_output_truncated"
down_revision = "0004_investigations_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execution_runs",
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("execution_runs", "truncated")
