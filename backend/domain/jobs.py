"""Background job repository and queue facade."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from backend.infra.events import RuntimeEventBus
from backend.infra.models import BackgroundJob


class BackgroundJobService:
    def __init__(self, *, events: RuntimeEventBus | None = None) -> None:
        self.events = events

    def create(
        self,
        db: Session,
        *,
        task_name: str,
        payload: dict[str, Any] | None = None,
        triggered_by: str = "",
        cron_job_id: uuid.UUID | None = None,
    ) -> BackgroundJob:
        job = BackgroundJob(
            task_name=task_name,
            payload=payload or {},
            triggered_by=triggered_by,
            cron_job_id=cron_job_id,
            status="queued",
        )
        db.add(job)
        db.flush()
        if self.events:
            self.events.emit(
                "job.created",
                {"job_id": str(job.id), "task_name": task_name, "triggered_by": triggered_by},
            )
            self.events.audit(
                "job.create",
                "background_job",
                target_id=str(job.id),
                payload={"task_name": task_name, "triggered_by": triggered_by},
            )
        return job

    def mark_enqueued(self, db: Session, job_id: uuid.UUID, *, queue_id: str) -> BackgroundJob:
        job = self._get(db, job_id)
        job.queue_id = queue_id
        job.status = "queued"
        if self.events:
            self.events.emit("job.enqueued", {"job_id": str(job.id), "queue_id": queue_id})
        return job

    def mark_started(self, db: Session, job_id: uuid.UUID) -> BackgroundJob:
        job = self._get(db, job_id)
        job.status = "running"
        job.started_at = datetime.now(UTC)
        job.error = ""
        if self.events:
            self.events.emit("job.started", {"job_id": str(job.id), "task_name": job.task_name})
        return job

    def mark_succeeded(self, db: Session, job_id: uuid.UUID, result: dict[str, Any]) -> BackgroundJob:
        job = self._get(db, job_id)
        job.status = "succeeded"
        job.result = result
        job.finished_at = datetime.now(UTC)
        if self.events:
            self.events.emit("job.succeeded", {"job_id": str(job.id), "task_name": job.task_name})
        return job

    def mark_failed(self, db: Session, job_id: uuid.UUID, error: str) -> BackgroundJob:
        job = self._get(db, job_id)
        job.status = "failed"
        job.error = error
        job.finished_at = datetime.now(UTC)
        if self.events:
            self.events.emit(
                "job.failed",
                {"job_id": str(job.id), "task_name": job.task_name, "error": error},
                severity="warning",
            )
        return job

    def cancel(self, db: Session, job_id: uuid.UUID) -> BackgroundJob:
        job = self._get(db, job_id)
        if job.status not in {"queued", "running"}:
            raise ValueError(f"job is not cancellable: {job.status}")
        if job.queue_id:
            try:
                from celery.result import AsyncResult

                from backend.worker.celery_app import celery_app

                AsyncResult(job.queue_id, app=celery_app).revoke(terminate=False)
            except ModuleNotFoundError:
                pass
        job.status = "cancelled"
        job.finished_at = datetime.now(UTC)
        if self.events:
            self.events.emit("job.cancelled", {"job_id": str(job.id), "task_name": job.task_name})
            self.events.audit("job.cancel", "background_job", target_id=str(job.id))
        return job

    def list(self, db: Session, *, status: str | None = None, limit: int = 100) -> list[BackgroundJob]:
        stmt = select(BackgroundJob).order_by(desc(BackgroundJob.created_at)).limit(max(1, min(limit, 500)))
        if status:
            stmt = stmt.where(BackgroundJob.status == status)
        return list(db.scalars(stmt).all())

    def get(self, db: Session, job_id: uuid.UUID) -> BackgroundJob | None:
        return db.get(BackgroundJob, job_id)

    def _get(self, db: Session, job_id: uuid.UUID) -> BackgroundJob:
        job = self.get(db, job_id)
        if job is None:
            raise KeyError(f"background job not found: {job_id}")
        return job


def enqueue_background_job(
    db: Session,
    *,
    task_name: str,
    payload: dict[str, Any] | None = None,
    triggered_by: str = "",
    cron_job_id: uuid.UUID | None = None,
    service: BackgroundJobService | None = None,
) -> BackgroundJob:
    service = service or BackgroundJobService()
    queue_id = str(uuid.uuid4())
    job = service.create(
        db,
        task_name=task_name,
        payload=payload or {},
        triggered_by=triggered_by,
        cron_job_id=cron_job_id,
    )
    service.mark_enqueued(db, job.id, queue_id=queue_id)
    from backend.worker.tasks import dispatch_task

    try:
        dispatch_task(task_name, str(job.id), payload or {}, queue_id=queue_id)
    except Exception as exc:
        service.mark_failed(db, job.id, str(exc))
        raise
    return job
