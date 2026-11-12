"""Alembic environment.

The database URL comes from the application settings (``data/ssh_suite.db`` by
default) rather than ``alembic.ini`` so there is a single source of truth. Set
``SSHSUITE_DATABASE_URL`` to migrate a different database.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app.models  # noqa: E402,F401  (registers the tables)
from app.core.config import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

settings = get_settings()
if not config.get_main_option("sqlalchemy.url"):
    # Only the application's own default database lives under the app's data
    # directory, so only that case needs the tree created. When the caller passes
    # an explicit URL (the test suite, CI, or ``alembic -x``) the target is
    # somewhere else entirely, and creating the app's ``data/`` and ``logs/``
    # directories here would write into the developer's real state as a side
    # effect of migrating an unrelated temporary database.
    settings.ensure_directories()
    config.set_main_option("sqlalchemy.url", settings.resolved_database_url)

target_metadata = Base.metadata


def _configure(connection=None, url: str | None = None) -> None:
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        literal_binds=connection is None,
        # batch mode keeps future ALTERs working on SQLite.
        render_as_batch=True,
        compare_type=True,
    )


def run_migrations_offline() -> None:
    _configure(url=config.get_main_option("sqlalchemy.url"))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
