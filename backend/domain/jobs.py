"""Background job repository and queue facade."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, event, or_, select
from sqlalchemy.orm import Session

from backend.infra.events import RuntimeEventBus
from backend.infra.models import BackgroundJob, IdempotencyRecord, OutboxMessage
from backend.infra.trace import current_request_id, current_trace_id


TERMINAL_JOB_STATUSES = {"succeeded", "failed", "dead_letter", "cancelled"}
RETRYABLE_JOB_STATUSES = {"queued", "retrying"}


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
        idempotency_key: str = "",
        max_attempts: int = 3,
    ) -> BackgroundJob:
        if idempotency_key:
            existing = db.scalar(select(BackgroundJob).where(BackgroundJob.idempotency_key == idempotency_key))
            if existing is not None:
                if self.events:
                    self.events.emit(
                        "job.idempotent_replay",
                        {"job_id": str(existing.id), "task_name": existing.task_name, "idempotency_key": idempotency_key},
                    )
                return existing
        job = BackgroundJob(
            task_name=task_name,
            trace_id=current_trace_id(),
            request_id=current_request_id(),
            payload=payload or {},
            triggered_by=triggered_by,
            cron_job_id=cron_job_id,
            status="queued",
            idempotency_key=idempotency_key,
            max_attempts=max(1, int(max_attempts or 3)),
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
                payload={"task_name": task_name, "triggered_by": triggered_by, "idempotency_key": idempotency_key},
            )
        return job

    def mark_enqueued(self, db: Session, job_id: uuid.UUID, *, queue_id: str) -> BackgroundJob:
        job = self._get(db, job_id)
        job.queue_id = queue_id
        job.status = "queued"
        if self.events:
            self.events.emit("job.enqueued", {"job_id": str(job.id), "queue_id": queue_id})
        return job

    def mark_started(self, db: Session, job_id: uuid.UUID, *, lock_owner: str = "") -> BackgroundJob:
        job = self._get(db, job_id)
        if job.status in TERMINAL_JOB_STATUSES:
            return job
        job.status = "running"
        job.attempt_count = int(job.attempt_count or 0) + 1
        job.started_at = datetime.now(UTC)
        job.finished_at = None
        job.error = ""
        job.locked_at = datetime.now(UTC)
        job.lock_owner = lock_owner
        if self.events:
            self.events.emit(
                "job.started",
                {"job_id": str(job.id), "task_name": job.task_name, "attempt": job.attempt_count},
            )
        return job

    def mark_succeeded(self, db: Session, job_id: uuid.UUID, result: dict[str, Any]) -> BackgroundJob:
        job = self._get(db, job_id)
        job.status = "succeeded"
        job.result = result
        job.finished_at = datetime.now(UTC)
        job.next_retry_at = None
        job.dead_letter_reason = ""
        job.locked_at = None
        job.lock_owner = ""
        if job.idempotency_key:
            self.complete_idempotency(
                db,
                scope="background_job",
                idempotency_key=job.idempotency_key,
                response=job_to_response(job),
                status="succeeded",
            )
        if self.events:
            self.events.emit("job.succeeded", {"job_id": str(job.id), "task_name": job.task_name})
        return job

    def mark_failed(self, db: Session, job_id: uuid.UUID, error: str) -> BackgroundJob:
        job = self._get(db, job_id)
        job.status = "failed"
        job.error = error
        job.finished_at = datetime.now(UTC)
        job.locked_at = None
        job.lock_owner = ""
        if self.events:
            self.events.emit(
                "job.failed",
                {"job_id": str(job.id), "task_name": job.task_name, "error": error},
                severity="warning",
            )
        return job

    def mark_retrying(self, db: Session, job_id: uuid.UUID, error: str, *, delay_seconds: int | None = None) -> BackgroundJob:
        job = self._get(db, job_id)
        delay = delay_seconds if delay_seconds is not None else _retry_delay_seconds(int(job.attempt_count or 0))
        job.status = "retrying"
        job.error = error
        job.next_retry_at = datetime.now(UTC) + timedelta(seconds=delay)
        job.locked_at = None
        job.lock_owner = ""
        if self.events:
            self.events.emit(
                "job.retry_scheduled",
                {"job_id": str(job.id), "task_name": job.task_name, "attempt": job.attempt_count, "delay_seconds": delay},
                severity="warning",
            )
        return job

    def mark_dead_letter(self, db: Session, job_id: uuid.UUID, error: str) -> BackgroundJob:
        job = self._get(db, job_id)
        job.status = "dead_letter"
        job.error = error
        job.dead_letter_reason = error
        job.finished_at = datetime.now(UTC)
        job.next_retry_at = None
        job.locked_at = None
        job.lock_owner = ""
        if job.idempotency_key:
            self.complete_idempotency(
                db,
                scope="background_job",
                idempotency_key=job.idempotency_key,
                response=job_to_response(job),
                status="failed",
            )
        if self.events:
            self.events.emit(
                "job.dead_letter",
                {"job_id": str(job.id), "task_name": job.task_name, "error": error},
                severity="error",
            )
        return job

    def should_run(self, job: BackgroundJob, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        if job.status in TERMINAL_JOB_STATUSES or job.status == "running":
            return False
        if job.next_retry_at and _aware(job.next_retry_at) > now:
            return False
        return True

    def cancel(self, db: Session, job_id: uuid.UUID) -> BackgroundJob:
        job = self._get(db, job_id)
        if job.status not in {"queued", "retrying", "running"}:
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
        job.locked_at = None
        job.lock_owner = ""
        if self.events:
            self.events.emit("job.cancelled", {"job_id": str(job.id), "task_name": job.task_name})
            self.events.audit("job.cancel", "background_job", target_id=str(job.id))
        return job

    def list(self, db: Session, *, status: str | None = None, limit: int = 100) -> list[BackgroundJob]:
        stmt = select(BackgroundJob).order_by(desc(BackgroundJob.created_at)).limit(max(1, min(limit, 500)))
        if status:
            stmt = stmt.where(BackgroundJob.status == status)
        return list(db.scalars(stmt).all())

    def ready_for_retry(self, db: Session, *, now: datetime | None = None, limit: int = 100) -> list[BackgroundJob]:
        now = now or datetime.now(UTC)
        stmt = (
            select(BackgroundJob)
            .where(BackgroundJob.status == "retrying")
            .where(or_(BackgroundJob.next_retry_at.is_(None), BackgroundJob.next_retry_at <= now))
            .order_by(BackgroundJob.next_retry_at.asc().nullsfirst(), BackgroundJob.created_at)
            .limit(max(1, min(limit, 500)))
        )
        return list(db.scalars(stmt).all())

    def get(self, db: Session, job_id: uuid.UUID) -> BackgroundJob | None:
        return db.get(BackgroundJob, job_id)

    def create_outbox(
        self,
        db: Session,
        *,
        job: BackgroundJob,
        queue_id: str,
        idempotency_key: str,
    ) -> OutboxMessage:
        existing = db.scalar(select(OutboxMessage).where(OutboxMessage.idempotency_key == idempotency_key))
        if existing is not None:
            return existing
        message = OutboxMessage(
            topic="celery.dispatch",
            trace_id=job.trace_id,
            request_id=job.request_id,
            aggregate_type="background_job",
            aggregate_id=str(job.id),
            idempotency_key=idempotency_key,
            status="pending",
            payload={
                "task_name": job.task_name,
                "job_id": str(job.id),
                "payload": job.payload or {},
                "queue_id": queue_id,
                "trace_id": job.trace_id,
                "request_id": job.request_id,
            },
            max_attempts=5,
        )
        db.add(message)
        db.flush()
        if self.events:
            self.events.emit("outbox.created", {"outbox_id": str(message.id), "job_id": str(job.id), "topic": message.topic})
        return message

    def schedule_dispatch(self, db: Session, job: BackgroundJob, *, reason: str = "enqueue") -> OutboxMessage:
        queue_id = str(uuid.uuid4())
        self.mark_enqueued(db, job.id, queue_id=queue_id)
        key_base = job.idempotency_key or f"job:{job.id}"
        outbox = self.create_outbox(
            db,
            job=job,
            queue_id=queue_id,
            idempotency_key=f"dispatch:{key_base}:{reason}:{int(job.attempt_count or 0)}",
        )
        _dispatch_outbox_after_commit(db, self, outbox.id)
        return outbox

    def reschedule_job(self, db: Session, job: BackgroundJob, *, reason: str = "retry") -> OutboxMessage:
        if job.status not in RETRYABLE_JOB_STATUSES:
            raise ValueError(f"job is not retryable: {job.status}")
        return self.schedule_dispatch(db, job, reason=reason)

    def dispatch_outbox(self, db: Session, *, limit: int = 100) -> dict[str, int]:
        now = datetime.now(UTC)
        stmt = (
            select(OutboxMessage)
            .where(OutboxMessage.status.in_(["pending", "retrying"]))
            .where(or_(OutboxMessage.next_attempt_at.is_(None), OutboxMessage.next_attempt_at <= now))
            .order_by(OutboxMessage.created_at)
            .limit(max(1, min(limit, 500)))
        )
        items = list(db.scalars(stmt).all())
        dispatched = 0
        failed = 0
        dead_letter = 0
        for message in items:
            try:
                self._dispatch_outbox_message(db, message)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                if message.status == "dead_letter":
                    dead_letter += 1
                if self.events:
                    self.events.emit(
                        "outbox.dispatch.failed",
                        {"outbox_id": str(message.id), "error": str(exc), "status": message.status},
                        severity="warning",
                    )
            else:
                dispatched += 1
        return {"scanned": len(items), "dispatched": dispatched, "failed": failed, "dead_letter": dead_letter}

    def _dispatch_outbox_message(self, db: Session, message: OutboxMessage) -> None:
        if message.topic != "celery.dispatch":
            raise ValueError(f"unsupported outbox topic: {message.topic}")
        payload = message.payload or {}
        from backend.worker.tasks import dispatch_task

        message.attempt_count = int(message.attempt_count or 0) + 1
        try:
            dispatch_task(
                str(payload.get("task_name") or ""),
                str(payload.get("job_id") or ""),
                payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
                queue_id=str(payload.get("queue_id") or ""),
                trace_id=str(payload.get("trace_id") or message.trace_id or ""),
                request_id=str(payload.get("request_id") or message.request_id or ""),
            )
        except Exception as exc:
            message.last_error = str(exc)
            if int(message.attempt_count or 0) >= int(message.max_attempts or 5):
                message.status = "dead_letter"
                message.dispatched_at = datetime.now(UTC)
                try:
                    self.mark_dead_letter(db, uuid.UUID(str(message.aggregate_id)), str(exc))
                except Exception:
                    pass
            else:
                message.status = "retrying"
                message.next_attempt_at = datetime.now(UTC) + timedelta(seconds=_retry_delay_seconds(int(message.attempt_count or 0)))
            raise
        message.status = "dispatched"
        message.last_error = ""
        message.next_attempt_at = None
        message.dispatched_at = datetime.now(UTC)
        if self.events:
            self.events.emit("outbox.dispatched", {"outbox_id": str(message.id), "topic": message.topic})

    def idempotency_status(
        self,
        db: Session,
        *,
        scope: str,
        idempotency_key: str,
        request_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not idempotency_key:
            return {"replayed": False, "conflict": False, "record": None}
        request_hash = _stable_hash(request_payload or {})
        record = db.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
        )
        if record is None:
            record = IdempotencyRecord(
                scope=scope,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                status="processing",
            )
            db.add(record)
            db.flush()
            return {"replayed": False, "conflict": False, "record": record}
        if record.request_hash and request_hash and record.request_hash != request_hash:
            return {"replayed": False, "conflict": True, "record": record}
        return {"replayed": record.status in {"succeeded", "failed"}, "conflict": False, "record": record}

    def complete_idempotency(
        self,
        db: Session,
        *,
        scope: str,
        idempotency_key: str,
        response: dict[str, Any],
        status: str = "succeeded",
    ) -> IdempotencyRecord | None:
        if not idempotency_key:
            return None
        record = db.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
        )
        if record is None:
            record = IdempotencyRecord(scope=scope, idempotency_key=idempotency_key)
            db.add(record)
        record.status = status
        record.response = response
        return record

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
    idempotency_key: str = "",
    max_attempts: int = 3,
) -> BackgroundJob:
    service = service or BackgroundJobService()
    payload = payload or {}
    if not idempotency_key:
        idempotency_key = _job_idempotency_key(
            task_name=task_name,
            payload=payload,
            triggered_by=triggered_by,
            cron_job_id=cron_job_id,
        )
    status = service.idempotency_status(
        db,
        scope="background_job",
        idempotency_key=idempotency_key,
        request_payload={"task_name": task_name, "payload": payload, "triggered_by": triggered_by, "cron_job_id": str(cron_job_id or "")},
    )
    if status["conflict"]:
        raise ValueError(f"idempotency key reused with different payload: {idempotency_key}")
    existing = db.scalar(select(BackgroundJob).where(BackgroundJob.idempotency_key == idempotency_key))
    if existing is not None:
        return existing
    job = service.create(
        db,
        task_name=task_name,
        payload=payload,
        triggered_by=triggered_by,
        cron_job_id=cron_job_id,
        idempotency_key=idempotency_key,
        max_attempts=max_attempts,
    )
    service.schedule_dispatch(db, job, reason="enqueue")
    return job


def replay_outbox(*, service: BackgroundJobService | None = None, limit: int = 100) -> dict[str, int]:
    from backend.infra.db import session_scope

    service = service or BackgroundJobService()
    with session_scope() as db:
        return service.dispatch_outbox(db, limit=limit)


def job_to_response(job: BackgroundJob) -> dict[str, Any]:
    return {
        "job_id": str(job.id),
        "task_name": job.task_name,
        "queue_id": job.queue_id,
        "status": job.status,
        "trace_id": getattr(job, "trace_id", "") or "",
        "request_id": getattr(job, "request_id", "") or "",
        "result": job.result or {},
        "error": job.error,
    }


def _dispatch_outbox_after_commit(db: Session, service: BackgroundJobService, outbox_id: uuid.UUID) -> None:
    if not hasattr(db, "dispatch"):
        try:
            service.dispatch_outbox(db, limit=100)
        except Exception:
            # The outbox row remains pending and can be replayed by scheduler/worker.
            pass
        return

    pending_key = "_soulclaw_after_commit_outbox_ids"
    pending = db.info.setdefault(pending_key, set())
    pending.add(str(outbox_id))
    listener_key = "_soulclaw_after_commit_outbox_listener"
    if db.info.get(listener_key):
        return
    db.info[listener_key] = True

    @event.listens_for(db, "after_commit", once=True)
    def _after_commit(session):  # pragma: no cover - covered through integration-style behavior
        ids = list(session.info.pop(pending_key, set()))
        session.info.pop(listener_key, None)
        if not ids:
            return
        try:
            replay_outbox(service=BackgroundJobService(), limit=max(100, len(ids)))
        except Exception:
            pass


def _job_idempotency_key(
    *,
    task_name: str,
    payload: dict[str, Any],
    triggered_by: str,
    cron_job_id: uuid.UUID | None,
) -> str:
    base = {
        "task_name": task_name,
        "payload": payload,
        "triggered_by": triggered_by,
        "cron_job_id": str(cron_job_id or ""),
    }
    return f"job:{task_name}:{_stable_hash(base)}"


def _stable_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _retry_delay_seconds(attempt_count: int) -> int:
    attempt = max(1, attempt_count)
    return min(300, 2 ** min(attempt, 8))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
