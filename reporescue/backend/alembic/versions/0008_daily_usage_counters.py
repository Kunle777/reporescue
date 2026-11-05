"""Persistent daily AI and Debug usage counters.

Revision ID: 0008_daily_usage_counters
Revises: 0007_preselect_and_run_command
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_daily_usage_counters"
down_revision = "0007_preselect_and_run_command"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_usage_counters",
        sa.Column("scope_key", sa.String(length=100), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("quota_type", sa.String(length=20), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint(
            "scope_key", "usage_date", "quota_type",
            name="pk_daily_usage_counters",
        ),
        sa.CheckConstraint("used >= 0", name="ck_daily_usage_used_nonnegative"),
    )


def downgrade() -> None:
    op.drop_table("daily_usage_counters")
