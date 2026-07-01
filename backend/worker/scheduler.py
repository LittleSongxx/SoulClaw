"""Standalone scheduler process for enqueuing due CronJob rows."""

from __future__ import annotations

import time

from loguru import logger

from backend.domain.jobs import BackgroundJobService
from backend.infra.config import get_settings
from backend.infra.db import run_alembic_upgrade
from backend.infra.events import RuntimeEventBus
from backend.runtime.cron import CronScheduler
from backend.worker.guards import ensure_background_database


class NoopAgent:
    def run_turn(self, *args, **kwargs):  # pragma: no cover - scheduler should only enqueue jobs
        raise RuntimeError("standalone scheduler does not run agent turns")


def main() -> None:
    settings = get_settings()
    ensure_background_database(settings, component="scheduler")
    if settings.auto_migrate:
        run_alembic_upgrade(settings)
    events = RuntimeEventBus()
    scheduler = CronScheduler(
        agent=NoopAgent(),
        events=events,
        jobs=BackgroundJobService(events=events),
        poll_interval_seconds=15.0,
    )
    scheduler.schedule_missing()
    logger.info("[scheduler] started")
    while True:
        try:
            scheduler.tick()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[scheduler] tick failed: {}", exc)
            events.emit("scheduler.tick.failed", {"error": str(exc)}, severity="warning")
        time.sleep(scheduler.poll_interval_seconds)


if __name__ == "__main__":
    main()
