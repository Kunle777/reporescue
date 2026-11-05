"""Add durable idempotency records and conversation grounding metadata.

Revision ID: 0009_conversation_idempotency
Revises: 0008_daily_usage_counters
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_conversation_idempotency"
down_revision = "0008_daily_usage_counters"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=160), nullable=False),
        sa.Column("request_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="processing"),
        sa.Column("response", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('processing', 'completed')", name="ck_idempotency_status"),
        sa.ForeignKeyConstraint(["owner_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "scope", "request_key", name="uq_idempotency_owner_scope_key"),
    )
    op.create_index("ix_idempotency_records_owner_id", "idempotency_records", ["owner_id"])

    with op.batch_alter_table("investigation_messages") as batch_op:
        batch_op.add_column(sa.Column("grounding_status", sa.String(length=24), nullable=True))
        batch_op.add_column(sa.Column("evidence_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("investigation_messages") as batch_op:
        batch_op.drop_column("evidence_ids")
        batch_op.drop_column("grounding_status")
    op.drop_index("ix_idempotency_records_owner_id", table_name="idempotency_records")
    op.drop_table("idempotency_records")
