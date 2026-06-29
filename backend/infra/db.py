"""Database session and Alembic helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
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
        from .models import Base

        _ensure_sqlite_parent(settings.database_url)
        engine = get_engine() if settings.database_url == get_settings().database_url else create_engine(
            settings.database_url,
            pool_pre_ping=True,
            future=True,
            connect_args=_connect_args(settings.database_url),
        )
        has_alembic_version = _sqlite_has_alembic_version(engine)
        Base.metadata.create_all(engine)
        if has_alembic_version:
            command.upgrade(_alembic_config(settings), "head")
            _ensure_sqlite_additive_schema(engine)
            return
        _ensure_sqlite_additive_schema(engine)
        command.stamp(_alembic_config(settings), "head")
        return
    command.upgrade(_alembic_config(settings), "head")


def migration_status(settings: Settings | None = None) -> dict[str, str | bool | None]:
    settings = settings or get_settings()
    _ensure_sqlite_parent(settings.database_url)
    engine = get_engine() if settings.database_url == get_settings().database_url else create_engine(
        settings.database_url,
        pool_pre_ping=True,
        future=True,
        connect_args=_connect_args(settings.database_url),
    )
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


def _sqlite_has_alembic_version(engine: Engine) -> bool:
    return "alembic_version" in inspect(engine).get_table_names()


def _ensure_sqlite_additive_schema(engine: Engine) -> None:
    """Add columns introduced after the initial SQLite create_all path."""

    columns: dict[str, list[tuple[str, str]]] = {
        "wiki_error_book": [
            ("constraint_rule", "TEXT NOT NULL DEFAULT ''"),
            ("verification_method", "TEXT NOT NULL DEFAULT ''"),
            ("source_refs", "JSON NOT NULL DEFAULT '[]'"),
            ("lifecycle_status", "VARCHAR(32) NOT NULL DEFAULT 'open'"),
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
        "mcp_servers": [
            ("config_source", "VARCHAR(256) NOT NULL DEFAULT 'manual'"),
            ("permission_policy", "JSON NOT NULL DEFAULT '{}'"),
            ("session_mode", "VARCHAR(32) NOT NULL DEFAULT 'transient'"),
            ("health_status", "VARCHAR(32) NOT NULL DEFAULT 'unknown'"),
            ("last_imported_checksum", "VARCHAR(128) NOT NULL DEFAULT ''"),
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
