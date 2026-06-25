"""Cron schedule helpers shared by control plane and runtime."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from croniter import croniter


def compute_next_run(cron_expr: str, timezone: str, *, base: datetime | None = None) -> datetime:
    tz = ZoneInfo(timezone)
    base_time = base or datetime.now(UTC)
    if base_time.tzinfo is None:
        base_time = base_time.replace(tzinfo=UTC)
    local_base = base_time.astimezone(tz)
    next_local = croniter(cron_expr, local_base).get_next(datetime)
    if next_local.tzinfo is None:
        next_local = next_local.replace(tzinfo=tz)
    return next_local.astimezone(UTC)

