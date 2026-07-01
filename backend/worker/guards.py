"""Runtime guards for background worker processes."""

from __future__ import annotations

from backend.infra.config import Settings
from backend.infra.db import database_backend


def ensure_background_database(settings: Settings, *, component: str) -> None:
    if settings.queue_eager:
        return
    if database_backend(settings.database_url) != "sqlite":
        return
    raise RuntimeError(
        f"SoulClaw {component} requires Postgres for background worker/scheduler mode; "
        "set SOULCLAW_DATABASE_URL to a postgresql+psycopg:// URL, or run only the single-process app with SQLite."
    )
