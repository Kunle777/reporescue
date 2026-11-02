from __future__ import annotations

"""Alembic environment: migrations run from the backend directory.

The target URL always comes from the backend environment (``DATABASE_URL``)
through :mod:`app.config`; the redacted target is printed before anything
runs. Offline mode (``alembic upgrade head --sql``) renders SQL without
connecting and is the required review step before touching Supabase.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alembic import context  # noqa: E402
from sqlalchemy import create_engine, pool  # noqa: E402

from app import config as app_config  # noqa: E402
from app.models import Base  # noqa: E402

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = app_config.get_database_url()
    print(f"Target: {app_config.redact_database_url(url)}")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = app_config.get_database_url()
    print(f"Target: {app_config.redact_database_url(url)}")
    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
