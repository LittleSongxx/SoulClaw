"""Redis adapter used as an optional hot cache/state layer."""

from __future__ import annotations

from loguru import logger
from redis import Redis

from .config import Settings, get_settings


def build_redis_client(settings: Settings | None = None) -> Redis | None:
    settings = settings or get_settings()
    try:
        client = Redis.from_url(settings.redis_url, decode_responses=True)
        client.ping()
        return client
    except Exception as exc:  # noqa: BLE001
        logger.warning("[redis] unavailable, continuing without hot cache: {}", exc)
        return None

