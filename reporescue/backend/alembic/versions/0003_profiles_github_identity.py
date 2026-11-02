"""Add verified GitHub identity columns to profiles.

Non-destructive: adds five nullable columns. Existing rows keep NULL
GitHub fields (disconnected), which is the correct state for
email/password accounts that never linked GitHub.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_profiles_github_identity"
down_revision = "0002_profiles_username"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("profiles", sa.Column("github_username", sa.String(100), nullable=True))
    op.add_column("profiles", sa.Column("github_user_id", sa.String(50), nullable=True))
    op.add_column("profiles", sa.Column("github_avatar_url", sa.String(2000), nullable=True))
    op.add_column("profiles", sa.Column("github_display_name", sa.String(200), nullable=True))
    op.add_column(
        "profiles", sa.Column("github_connected_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("profiles", "github_connected_at")
    op.drop_column("profiles", "github_display_name")
    op.drop_column("profiles", "github_avatar_url")
    op.drop_column("profiles", "github_user_id")
    op.drop_column("profiles", "github_username")
