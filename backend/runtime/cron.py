"""DB-backed Cron scheduler for the SoulClaw runtime."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import select

from backend.domain.cron_schedule import compute_next_run
from backend.domain.jobs import BackgroundJobService, enqueue_background_job
from backend.infra.db import session_scope
from backend.infra.events import RuntimeEventBus
from backend.infra.models import CronJob
from backend.runtime.agent import AgentRuntime
from backend.runtime.dream import DreamRuntime


class CronScheduler:
    def __init__(
        self,
        *,
        agent: AgentRuntime,
        events: RuntimeEventBus,
        dream: DreamRuntime | None = None,
        jobs: BackgroundJobService | None = None,
        poll_interval_seconds: float = 15.0,
    ) -> None:
        self.agent = agent
        self.events = events
        self.dream = dream
        self.jobs = jobs or BackgroundJobService(events=events)
        self.poll_interval_seconds = poll_interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._run(), name="soulclaw-cron-scheduler")
            self.events.emit("cron.scheduler.started", {"poll_interval_seconds": self.poll_interval_seconds})

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self.events.emit("cron.scheduler.stopped", {})

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.tick)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[cron] scheduler tick failed: {}", exc)
                self.events.emit("cron.scheduler.error", {"error": str(exc)}, severity="warning")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval_seconds)
            except TimeoutError:
                continue

    def tick(self, *, now: datetime | None = None, limit: int = 10) -> int:
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        with session_scope() as db:
            bind_session = getattr(self.events, "bind_session", None)
            with bind_session(db) if bind_session is not None else nullcontext():
                jobs = list(
                    db.scalars(
                        select(CronJob)
                        .where(CronJob.enabled.is_(True))
                        .where((CronJob.next_run_at.is_(None)) | (CronJob.next_run_at <= now))
                        .order_by(CronJob.next_run_at.asc().nullsfirst(), CronJob.name)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    ).all()
                )
                for job in jobs:
                    self._run_job(db, job, now=now)
                return len(jobs)

    def schedule_missing(self, *, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        with session_scope() as db:
            jobs = list(db.scalars(select(CronJob).where(CronJob.enabled.is_(True), CronJob.next_run_at.is_(None))).all())
            for job in jobs:
                job.next_run_at = compute_next_run(job.cron_expr, job.timezone, base=now)
            return len(jobs)

    def _run_job(self, db, job: CronJob, *, now: datetime) -> None:
        self.events.emit("cron.job.started", {"job_id": str(job.id), "name": job.name}, session_id=f"cron:{job.name}")
        try:
            if self._is_dream_review_job(job):
                background = enqueue_background_job(
                    db,
                    task_name="dream_review",
                    payload={
                        "window_hours": int((job.metadata_json or {}).get("window_hours") or 24),
                        "limit": int((job.metadata_json or {}).get("limit") or 50),
                    },
                    triggered_by=f"cron:{job.name}",
                    cron_job_id=job.id,
                    service=self.jobs,
                )
                result_payload = {
                    "mode": "enqueue",
                    "task_name": "dream_review",
                    "job_id": str(background.id),
                }
            elif self._is_heartbeat_job(job):
                background = enqueue_background_job(
                    db,
                    task_name="heartbeat_check",
                    payload={},
                    triggered_by=f"cron:{job.name}",
                    cron_job_id=job.id,
                    service=self.jobs,
                )
                result_payload = {
                    "mode": "enqueue",
                    "task_name": "heartbeat_check",
                    "job_id": str(background.id),
                }
            else:
                background = enqueue_background_job(
                    db,
                    task_name=str((job.metadata_json or {}).get("task_name") or "dream_review"),
                    payload=(job.metadata_json or {}).get("payload") if isinstance((job.metadata_json or {}).get("payload"), dict) else {},
                    triggered_by=f"cron:{job.name}",
                    cron_job_id=job.id,
                    service=self.jobs,
                )
                result_payload = {"mode": "enqueue", "task_name": background.task_name, "job_id": str(background.id)}
        except Exception as exc:  # noqa: BLE001
            job.last_status = "failed"
            job.failure_count = int(job.failure_count or 0) + 1
            job.last_result = {"error": str(exc)}
            self.events.emit(
                "cron.job.failed",
                {"job_id": str(job.id), "name": job.name, "error": str(exc)},
                severity="warning",
                session_id=f"cron:{job.name}",
            )
        else:
            job.last_status = "succeeded"
            job.last_result = result_payload
            self.events.emit(
                "cron.job.succeeded",
                {"job_id": str(job.id), "name": job.name, "mode": result_payload.get("mode", "agent_turn")},
                session_id=f"cron:{job.name}",
            )
        finally:
            job.last_run_at = now
            job.run_count = int(job.run_count or 0) + 1
            job.next_run_at = compute_next_run(job.cron_expr, job.timezone, base=now)

    @staticmethod
    def _is_dream_review_job(job: CronJob) -> bool:
        metadata = job.metadata_json or {}
        return metadata.get("system_task") == "dream_review"

    @staticmethod
    def _is_heartbeat_job(job: CronJob) -> bool:
        metadata = job.metadata_json or {}
        return metadata.get("system_task") == "heartbeat"
