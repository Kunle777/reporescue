"""Add durable idempotency failure states and refundable quota flags.

Revision ID: 0010_quota_refunds
Revises: 0009_conversation_idempotency
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_quota_refunds"
down_revision = "0009_conversation_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("idempotency_records") as batch_op:
        batch_op.add_column(
            sa.Column(
                "quota_refunded",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.drop_constraint("ck_idempotency_status", type_="check")
        batch_op.create_check_constraint(
            "ck_idempotency_status",
            "status IN ('processing', 'completed', 'failed')",
        )
    with op.batch_alter_table("investigations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "ai_quota_charged",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "debug_quota_charged",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("investigations") as batch_op:
        batch_op.drop_column("debug_quota_charged")
        batch_op.drop_column("ai_quota_charged")
    # 'failed' claims carry no completed response; deleting them returns
    # their keys to reusable instead of violating the restored check.
    op.execute("DELETE FROM idempotency_records WHERE status = 'failed'")
    with op.batch_alter_table("idempotency_records") as batch_op:
        batch_op.drop_constraint("ck_idempotency_status", type_="check")
        batch_op.create_check_constraint(
            "ck_idempotency_status", "status IN ('processing', 'completed')"
        )
        batch_op.drop_column("quota_refunded")
