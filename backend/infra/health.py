"""Runtime health and readiness checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from redis import Redis
from sqlalchemy import create_engine

from backend.infra.config import Settings, get_settings
from backend.infra.db import database_backend, get_engine, migration_status


def readiness_summary(settings: Settings, redis_client: Redis | None = None) -> dict[str, Any]:
    checks = {
        "database": _database_check(settings),
        "redis": _redis_check(redis_client, required=settings.redis_required),
        "migrations": _migration_check(settings),
        "vector": _vector_check(settings),
        "directories": _directory_check(settings),
    }
    return {
        "ok": all(item["ok"] for item in checks.values()),
        "database_backend": database_backend(settings.database_url),
        "checks": checks,
    }


def _database_check(settings: Settings) -> dict[str, Any]:
    try:
        engine = get_engine() if settings.database_url == get_settings().database_url else create_engine(
            settings.database_url,
            pool_pre_ping=True,
            future=True,
            connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
        )
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1").scalar()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _redis_check(redis_client: Redis | None, *, required: bool) -> dict[str, Any]:
    if redis_client is None:
        if not required:
            return {"ok": True, "configured": False, "required": False, "status": "optional"}
        return {"ok": False, "configured": True, "required": True, "error": "redis unavailable"}
    try:
        redis_client.ping()
        return {"ok": True, "configured": True, "required": required}
    except Exception as exc:  # noqa: BLE001
        if not required:
            return {
                "ok": True,
                "configured": True,
                "required": False,
                "status": "optional_unavailable",
                "error": str(exc),
            }
        return {"ok": False, "configured": True, "required": required, "error": str(exc)}


def _migration_check(settings: Settings) -> dict[str, Any]:
    try:
        status = migration_status(settings)
        return {
            "ok": bool(status["is_current"]),
            "current_revision": status["current_revision"],
            "head_revision": status["head_revision"],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _vector_check(settings: Settings) -> dict[str, Any]:
    if settings.vector_mode.lower() == "disabled":
        return {"ok": True, "enabled": False, "required": False, "status": "disabled"}
    backend = database_backend(settings.database_url)
    base = {
        "enabled": True,
        "required": settings.vector_required,
        "backend": backend,
        "embedding_model": settings.embedding_model,
        "embedding_dimensions": settings.embedding_dimensions,
        "embedding_configured": bool(settings.openai_api_key),
    }
    if backend == "sqlite":
        return {**base, "ok": not settings.vector_required, "status": "test_fallback"}
    if backend != "postgresql":
        return {**base, "ok": not settings.vector_required, "status": "unsupported_backend"}
    try:
        engine = get_engine() if settings.database_url == get_settings().database_url else create_engine(
            settings.database_url,
            pool_pre_ping=True,
            future=True,
        )
        with engine.connect() as connection:
            has_vector = bool(
                connection.exec_driver_sql("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')").scalar()
            )
        return {**base, "ok": has_vector, "pgvector": has_vector}
    except Exception as exc:  # noqa: BLE001
        return {**base, "ok": False, "error": str(exc)}


def _directory_check(settings: Settings) -> dict[str, Any]:
    paths = [
        settings.data_dir,
        settings.config_dir,
        settings.workspace_dir,
        settings.packages_dir,
        settings.resolved_wiki_root,
        settings.resolved_skills_root,
    ]
    items = [_writable_directory(path) for path in paths]
    return {"ok": all(item["ok"] for item in items), "items": items}


def _writable_directory(path: Path) -> dict[str, Any]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        marker = path / ".soulclaw-healthcheck"
        marker.write_text("ok", encoding="utf-8")
        marker.unlink(missing_ok=True)
        return {"ok": True, "path": str(path)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "path": str(path), "error": str(exc)}
