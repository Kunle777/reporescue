"""Add unique nullable username to profiles (auth UX).

Non-destructive: adds one nullable column plus a unique constraint.
Existing rows keep NULL usernames (OAuth-style), which PostgreSQL permits
any number of under a unique constraint.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_profiles_username"
down_revision = "0001_profiles_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("profiles", sa.Column("username", sa.String(30), nullable=True))
    op.create_unique_constraint("uq_profiles_username", "profiles", ["username"])


def downgrade() -> None:
    op.drop_constraint("uq_profiles_username", "profiles", type_="unique")
    op.drop_column("profiles", "username")
