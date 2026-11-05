"""General pre-classification columns and execution-run commands.

Revision ID: 0007_preselect_and_run_command
Revises: 0006_general_routing_snapshots
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_preselect_and_run_command"
down_revision = "0006_general_routing_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("investigations") as batch_op:
        batch_op.add_column(
            sa.Column("preselected_mode", sa.String(20), nullable=True)
        )
        batch_op.add_column(sa.Column("preselect_rationale", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("preselect_confidence", sa.String(20), nullable=True)
        )
        batch_op.add_column(
            sa.Column("preselect_source", sa.String(40), nullable=True)
        )
    with op.batch_alter_table("execution_runs") as batch_op:
        batch_op.add_column(sa.Column("command", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("execution_runs") as batch_op:
        batch_op.drop_column("command")
    with op.batch_alter_table("investigations") as batch_op:
        batch_op.drop_column("preselect_source")
        batch_op.drop_column("preselect_confidence")
        batch_op.drop_column("preselect_rationale")
        batch_op.drop_column("preselected_mode")
