from __future__ import annotations

from logging.config import fileConfig

from alembic.script import ScriptDirectory
from sqlalchemy import engine_from_config, pool

from alembic import context
from backend.infra.config import get_settings
from backend.infra.db import (
    ensure_alembic_version_table_capacity,
    reconcile_sqlite_schema,
    stamp_sqlite_revision,
)
from backend.infra.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    configured = config.get_main_option("sqlalchemy.url")
    if configured and configured != "sqlite:///data/soulclaw.sqlite3":
        return configured
    return get_settings().database_url


def _command_name() -> str:
    cmd = getattr(getattr(config, "cmd_opts", None), "cmd", None)
    if isinstance(cmd, tuple) and cmd:
        return getattr(cmd[0], "__name__", "")
    return ""


def _sqlite_requested_revision() -> str:
    cmd_opts = getattr(config, "cmd_opts", None)
    revision = getattr(cmd_opts, "revision", None) if cmd_opts is not None else None
    if isinstance(revision, str) and revision:
        return revision
    return "head"


def _head_revision() -> str | None:
    head = ScriptDirectory.from_config(config).get_current_head()
    return str(head) if head else None


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    database_url = _database_url()
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = database_url
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {},
    )

    if connectable.dialect.name == "sqlite":
        command_name = _command_name()
        if command_name in {"downgrade"}:
            raise RuntimeError("SQLite Alembic downgrade is not supported; restore a backup or rebuild the local database.")
        if command_name in {"", "upgrade", "stamp"}:
            requested_revision = _sqlite_requested_revision()
            if requested_revision not in {"head", "heads"}:
                raise RuntimeError("SQLite Alembic reconciliation only supports upgrade/stamp to head.")
            reconcile_sqlite_schema(connectable)
            stamp_sqlite_revision(connectable, _head_revision())
            connectable.dispose()
            return

    with connectable.connect() as connection:
        ensure_alembic_version_table_capacity(connection)
        if connection.in_transaction():
            connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
