"""Database session and Alembic helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command

from .config import Settings, get_settings


def _connect_args(database_url: str) -> dict[str, bool]:
    return {"check_same_thread": False} if database_url.startswith("sqlite") else {}


def _ensure_sqlite_parent(database_url: str) -> None:
    if not database_url.startswith("sqlite:///"):
        return
    database_path = database_url.removeprefix("sqlite:///")
    if database_path and database_path != ":memory:":
        Path(database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    _ensure_sqlite_parent(settings.database_url)
    return create_engine(settings.database_url, pool_pre_ping=True, future=True, connect_args=_connect_args(settings.database_url))


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False, future=True)


def new_session() -> Session:
    return get_session_factory()()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def run_alembic_upgrade(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if settings.database_url.startswith("sqlite"):
        engine = _engine_for_settings(settings)
        reconcile_sqlite_schema(engine)
        stamp_sqlite_revision(engine, _head_revision(settings))
        return
    command.upgrade(_alembic_config(settings), "head")


def migration_status(settings: Settings | None = None) -> dict[str, str | bool | None]:
    settings = settings or get_settings()
    engine = _engine_for_settings(settings)
    current_revision = _current_revision(engine)
    head_revision = _head_revision(settings)
    return {
        "database_backend": database_backend(settings.database_url),
        "current_revision": current_revision,
        "head_revision": head_revision,
        "is_current": bool(current_revision and current_revision == head_revision),
    }


def database_backend(database_url: str) -> str:
    if database_url.startswith("sqlite"):
        return "sqlite"
    if database_url.startswith("postgresql"):
        return "postgresql"
    return database_url.split(":", 1)[0] or "unknown"


def reset_db_caches() -> None:
    get_session_factory.cache_clear()
    get_engine.cache_clear()


def reconcile_sqlite_schema(engine: Engine) -> None:
    """Make a SQLite database match the SQLAlchemy model schema."""

    from .models import Base

    Base.metadata.create_all(engine)
    _ensure_sqlite_additive_schema(engine)


def stamp_sqlite_revision(engine: Engine, revision: str | None) -> None:
    """Record the reconciled Alembic revision without replaying PG-oriented DDL."""

    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(128) NOT NULL)")
        connection.exec_driver_sql("DELETE FROM alembic_version")
        if revision:
            connection.exec_driver_sql("INSERT INTO alembic_version (version_num) VALUES (?)", (revision,))


def ensure_alembic_version_table_capacity(connection: Connection) -> None:
    """Keep long SoulClaw revision ids from overflowing Alembic's default width."""

    if connection.dialect.name != "postgresql":
        return
    connection.exec_driver_sql("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(128) NOT NULL)")
    connection.exec_driver_sql("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(128)")


def _engine_for_settings(settings: Settings) -> Engine:
    _ensure_sqlite_parent(settings.database_url)
    return get_engine() if settings.database_url == get_settings().database_url else create_engine(
        settings.database_url,
        pool_pre_ping=True,
        future=True,
        connect_args=_connect_args(settings.database_url),
    )


def _alembic_config(settings: Settings) -> Config:
    config_path = Path("alembic.ini")
    cfg = Config(str(config_path))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    return cfg


def _head_revision(settings: Settings) -> str | None:
    directory = ScriptDirectory.from_config(_alembic_config(settings))
    head = directory.get_current_head()
    return str(head) if head else None


def _current_revision(engine: Engine) -> str | None:
    inspector = inspect(engine)
    if "alembic_version" not in inspector.get_table_names():
        return None
    with engine.connect() as connection:
        row = connection.exec_driver_sql("SELECT version_num FROM alembic_version").first()
    return str(row[0]) if row and row[0] else None


def _ensure_sqlite_additive_schema(engine: Engine) -> None:
    """Add columns introduced after the initial SQLite create_all path."""

    columns: dict[str, list[tuple[str, str]]] = {
        "cron_jobs": [
            ("last_run_at", "DATETIME"),
            ("next_run_at", "DATETIME"),
            ("last_status", "VARCHAR(32) NOT NULL DEFAULT 'never_run'"),
            ("last_result", "JSON NOT NULL DEFAULT '{}'"),
            ("run_count", "INTEGER NOT NULL DEFAULT 0"),
            ("failure_count", "INTEGER NOT NULL DEFAULT 0"),
            ("backoff_until", "DATETIME"),
            ("last_enqueue_key", "VARCHAR(256) NOT NULL DEFAULT ''"),
        ],
        "mcp_servers": [
            ("status", "VARCHAR(32) NOT NULL DEFAULT 'disabled'"),
            ("last_connected_at", "DATETIME"),
            ("last_error", "TEXT NOT NULL DEFAULT ''"),
            ("tool_count", "INTEGER NOT NULL DEFAULT 0"),
            ("tools_cache", "JSON NOT NULL DEFAULT '[]'"),
            ("config_source", "VARCHAR(256) NOT NULL DEFAULT 'manual'"),
            ("permission_policy", "JSON NOT NULL DEFAULT '{}'"),
            ("session_mode", "VARCHAR(32) NOT NULL DEFAULT 'transient'"),
            ("health_status", "VARCHAR(32) NOT NULL DEFAULT 'unknown'"),
            ("last_imported_checksum", "VARCHAR(128) NOT NULL DEFAULT ''"),
        ],
        "gateway_connections": [
            ("last_inbound_at", "DATETIME"),
            ("last_outbound_at", "DATETIME"),
            ("last_heartbeat_at", "DATETIME"),
            ("instance_id", "VARCHAR(128) NOT NULL DEFAULT ''"),
            ("version", "VARCHAR(80) NOT NULL DEFAULT ''"),
            ("capabilities", "JSON NOT NULL DEFAULT '[]'"),
            ("last_error", "TEXT NOT NULL DEFAULT ''"),
            ("inbound_count", "INTEGER NOT NULL DEFAULT 0"),
            ("outbound_count", "INTEGER NOT NULL DEFAULT 0"),
            ("failure_count", "INTEGER NOT NULL DEFAULT 0"),
        ],
        "wiki_error_book": [
            ("constraint_rule", "TEXT NOT NULL DEFAULT ''"),
            ("verification_method", "TEXT NOT NULL DEFAULT ''"),
            ("source_refs", "JSON NOT NULL DEFAULT '[]'"),
            ("lifecycle_status", "VARCHAR(32) NOT NULL DEFAULT 'open'"),
        ],
        "wiki_pages": [
            ("claims", "JSON NOT NULL DEFAULT '[]'"),
            ("source_refs", "JSON NOT NULL DEFAULT '[]'"),
            ("stale_after", "DATETIME"),
        ],
        "memories": [
            ("status", "VARCHAR(32) NOT NULL DEFAULT 'active'"),
            ("superseded_by", "CHAR(32)"),
            ("source_file_marker", "VARCHAR(256) NOT NULL DEFAULT ''"),
            ("valid_from", "DATETIME"),
            ("valid_to", "DATETIME"),
            ("provenance", "JSON NOT NULL DEFAULT '{}'"),
        ],
        "approvals": [
            ("turn_checkpoint", "JSON NOT NULL DEFAULT '{}'"),
            ("original_tool_call", "JSON NOT NULL DEFAULT '{}'"),
            ("allowed_decisions", "JSON NOT NULL DEFAULT '[]'"),
            ("edited_arguments", "JSON NOT NULL DEFAULT '{}'"),
            ("resume_state", "JSON NOT NULL DEFAULT '{}'"),
        ],
        "evolution_proposals": [
            ("target_checksum", "VARCHAR(128) NOT NULL DEFAULT ''"),
            ("stale_reason", "TEXT NOT NULL DEFAULT ''"),
        ],
        "background_jobs": [
            ("trace_id", "VARCHAR(128) NOT NULL DEFAULT ''"),
            ("request_id", "VARCHAR(128) NOT NULL DEFAULT ''"),
            ("idempotency_key", "VARCHAR(256) NOT NULL DEFAULT ''"),
            ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
            ("max_attempts", "INTEGER NOT NULL DEFAULT 3"),
            ("next_retry_at", "DATETIME"),
            ("locked_at", "DATETIME"),
            ("lock_owner", "VARCHAR(128) NOT NULL DEFAULT ''"),
            ("dead_letter_reason", "TEXT NOT NULL DEFAULT ''"),
        ],
        "runtime_events": [
            ("trace_id", "VARCHAR(128) NOT NULL DEFAULT ''"),
            ("request_id", "VARCHAR(128) NOT NULL DEFAULT ''"),
        ],
        "audit_events": [
            ("trace_id", "VARCHAR(128) NOT NULL DEFAULT ''"),
            ("request_id", "VARCHAR(128) NOT NULL DEFAULT ''"),
        ],
        "outbox_messages": [
            ("trace_id", "VARCHAR(128) NOT NULL DEFAULT ''"),
            ("request_id", "VARCHAR(128) NOT NULL DEFAULT ''"),
            ("topic", "VARCHAR(128) NOT NULL DEFAULT ''"),
            ("aggregate_type", "VARCHAR(64) NOT NULL DEFAULT ''"),
            ("aggregate_id", "VARCHAR(128) NOT NULL DEFAULT ''"),
            ("idempotency_key", "VARCHAR(256) NOT NULL DEFAULT ''"),
            ("status", "VARCHAR(32) NOT NULL DEFAULT 'pending'"),
            ("payload", "JSON NOT NULL DEFAULT '{}'"),
            ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
            ("max_attempts", "INTEGER NOT NULL DEFAULT 5"),
            ("next_attempt_at", "DATETIME"),
            ("last_error", "TEXT NOT NULL DEFAULT ''"),
            ("created_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
            ("dispatched_at", "DATETIME"),
        ],
        "idempotency_records": [
            ("scope", "VARCHAR(128) NOT NULL DEFAULT ''"),
            ("idempotency_key", "VARCHAR(256) NOT NULL DEFAULT ''"),
            ("request_hash", "VARCHAR(128) NOT NULL DEFAULT ''"),
            ("status", "VARCHAR(32) NOT NULL DEFAULT 'processing'"),
            ("response", "JSON NOT NULL DEFAULT '{}'"),
            ("created_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
            ("updated_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
            ("expires_at", "DATETIME"),
        ],
    }
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table_name, table_columns in columns.items():
            if table_name not in table_names:
                continue
            existing = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, ddl in table_columns:
                if column_name not in existing:
                    connection.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}")
        if "memory_history" not in table_names:
            connection.exec_driver_sql(
                """
                CREATE TABLE memory_history (
                    id CHAR(32) NOT NULL,
                    memory_id CHAR(32),
                    action VARCHAR(64) NOT NULL,
                    before_snapshot JSON NOT NULL DEFAULT '{}',
                    after_snapshot JSON NOT NULL DEFAULT '{}',
                    actor VARCHAR(80) NOT NULL DEFAULT 'system',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (id)
                )
                """
            )
        if "outbox_messages" not in table_names:
            connection.exec_driver_sql(
                """
                CREATE TABLE outbox_messages (
                    id CHAR(32) NOT NULL,
                    trace_id VARCHAR(128) NOT NULL DEFAULT '',
                    request_id VARCHAR(128) NOT NULL DEFAULT '',
                    topic VARCHAR(128) NOT NULL,
                    aggregate_type VARCHAR(64) NOT NULL,
                    aggregate_id VARCHAR(128) NOT NULL,
                    idempotency_key VARCHAR(256) NOT NULL DEFAULT '',
                    status VARCHAR(32) NOT NULL DEFAULT 'pending',
                    payload JSON NOT NULL DEFAULT '{}',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 5,
                    next_attempt_at DATETIME,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    dispatched_at DATETIME,
                    PRIMARY KEY (id)
                )
                """
            )
        if "idempotency_records" not in table_names:
            connection.exec_driver_sql(
                """
                CREATE TABLE idempotency_records (
                    id CHAR(32) NOT NULL,
                    scope VARCHAR(128) NOT NULL,
                    idempotency_key VARCHAR(256) NOT NULL,
                    request_hash VARCHAR(128) NOT NULL DEFAULT '',
                    status VARCHAR(32) NOT NULL DEFAULT 'processing',
                    response JSON NOT NULL DEFAULT '{}',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    expires_at DATETIME,
                    PRIMARY KEY (id)
                )
                """
            )
        for table_name, index_name, column_names, unique in _sqlite_additive_indexes():
            _ensure_sqlite_index(connection, table_name, index_name, column_names, unique=unique)


def _sqlite_additive_indexes() -> list[tuple[str, str, tuple[str, ...], bool]]:
    return [
        ("cron_jobs", "ix_cron_jobs_next_run_at", ("next_run_at",), False),
        ("cron_jobs", "ix_cron_jobs_last_status", ("last_status",), False),
        ("cron_jobs", "ix_cron_jobs_backoff_until", ("backoff_until",), False),
        ("cron_jobs", "ix_cron_jobs_last_enqueue_key", ("last_enqueue_key",), False),
        ("mcp_servers", "ix_mcp_servers_status", ("status",), False),
        ("mcp_servers", "ix_mcp_servers_config_source", ("config_source",), False),
        ("mcp_servers", "ix_mcp_servers_session_mode", ("session_mode",), False),
        ("mcp_servers", "ix_mcp_servers_health_status", ("health_status",), False),
        ("wiki_error_book", "ix_wiki_error_book_lifecycle_status", ("lifecycle_status",), False),
        ("wiki_pages", "ix_wiki_pages_stale_after", ("stale_after",), False),
        ("memories", "ix_memories_status", ("status",), False),
        ("memories", "ix_memories_superseded_by", ("superseded_by",), False),
        ("memories", "ix_memories_source_file_marker", ("source_file_marker",), False),
        ("evolution_proposals", "ix_evolution_proposals_target_checksum", ("target_checksum",), False),
        ("background_jobs", "ix_background_jobs_trace_id", ("trace_id",), False),
        ("background_jobs", "ix_background_jobs_request_id", ("request_id",), False),
        ("background_jobs", "ix_background_jobs_idempotency_key", ("idempotency_key",), False),
        ("background_jobs", "ix_background_jobs_next_retry_at", ("next_retry_at",), False),
        ("runtime_events", "ix_runtime_events_trace_id", ("trace_id",), False),
        ("runtime_events", "ix_runtime_events_request_id", ("request_id",), False),
        ("audit_events", "ix_audit_events_trace_id", ("trace_id",), False),
        ("audit_events", "ix_audit_events_request_id", ("request_id",), False),
        ("memory_history", "ix_memory_history_memory_id", ("memory_id",), False),
        ("memory_history", "ix_memory_history_action", ("action",), False),
        ("memory_history", "ix_memory_history_actor", ("actor",), False),
        ("memory_history", "ix_memory_history_created_at", ("created_at",), False),
        ("outbox_messages", "ix_outbox_messages_trace_id", ("trace_id",), False),
        ("outbox_messages", "ix_outbox_messages_request_id", ("request_id",), False),
        ("outbox_messages", "uq_outbox_messages_idempotency_key", ("idempotency_key",), True),
        ("outbox_messages", "ix_outbox_messages_idempotency_key", ("idempotency_key",), False),
        ("outbox_messages", "ix_outbox_messages_topic", ("topic",), False),
        ("outbox_messages", "ix_outbox_messages_aggregate_type", ("aggregate_type",), False),
        ("outbox_messages", "ix_outbox_messages_aggregate_id", ("aggregate_id",), False),
        ("outbox_messages", "ix_outbox_messages_status", ("status",), False),
        ("outbox_messages", "ix_outbox_messages_next_attempt_at", ("next_attempt_at",), False),
        ("outbox_messages", "ix_outbox_messages_created_at", ("created_at",), False),
        ("idempotency_records", "uq_idempotency_scope_key", ("scope", "idempotency_key"), True),
        ("idempotency_records", "ix_idempotency_records_scope", ("scope",), False),
        ("idempotency_records", "ix_idempotency_records_idempotency_key", ("idempotency_key",), False),
        ("idempotency_records", "ix_idempotency_records_status", ("status",), False),
        ("idempotency_records", "ix_idempotency_records_created_at", ("created_at",), False),
        ("idempotency_records", "ix_idempotency_records_expires_at", ("expires_at",), False),
    ]


def _ensure_sqlite_index(
    connection: Connection,
    table_name: str,
    index_name: str,
    column_names: tuple[str, ...],
    *,
    unique: bool = False,
) -> None:
    if not _sqlite_table_exists(connection, table_name):
        return
    existing_columns = _sqlite_table_columns(connection, table_name)
    if not set(column_names).issubset(existing_columns):
        return
    columns_sql = ", ".join(column_names)
    unique_sql = "UNIQUE " if unique else ""
    connection.exec_driver_sql(f"CREATE {unique_sql}INDEX IF NOT EXISTS {index_name} ON {table_name}({columns_sql})")


def _sqlite_table_exists(connection: Connection, table_name: str) -> bool:
    row = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).first()
    return row is not None


def _sqlite_table_columns(connection: Connection, table_name: str) -> set[str]:
    rows = connection.exec_driver_sql(f"PRAGMA table_info({table_name})").all()
    return {str(row[1]) for row in rows}
