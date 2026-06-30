from __future__ import annotations

import uuid

from backend.domain.jobs import BackgroundJobService, enqueue_background_job
from backend.infra.models import BackgroundJob, IdempotencyRecord, OutboxMessage
from backend.infra.trace import bind_trace_context


class FakeDB:
    def __init__(self) -> None:
        self.objects = []
        self.commits = 0

    def add(self, item) -> None:
        self.objects.append(item)

    def flush(self) -> None:
        for item in self.objects:
            if getattr(item, "id", None) is None:
                item.id = uuid.uuid4()

    def get(self, model, item_id):
        for item in self.objects:
            if isinstance(item, model) and item.id == item_id:
                return item
        return None

    def scalar(self, statement):
        text = str(statement)
        if "background_jobs" in text:
            key = _bound(statement, "idempotency_key_1")
            if key:
                return next((item for item in self.objects if isinstance(item, BackgroundJob) and item.idempotency_key == key), None)
        if "idempotency_records" in text:
            scope = _bound(statement, "scope_1")
            key = _bound(statement, "idempotency_key_1")
            return next(
                (
                    item
                    for item in self.objects
                    if isinstance(item, IdempotencyRecord) and item.scope == scope and item.idempotency_key == key
                ),
                None,
            )
        if "outbox_messages" in text:
            key = _bound(statement, "idempotency_key_1")
            return next((item for item in self.objects if isinstance(item, OutboxMessage) and item.idempotency_key == key), None)
        return None

    def scalars(self, statement):
        text = str(statement)
        if "outbox_messages" in text:
            return FakeResult([item for item in self.objects if isinstance(item, OutboxMessage) and item.status in {"pending", "retrying"}])
        return FakeResult([])

    def commit(self) -> None:
        self.commits += 1


class FakeResult:
    def __init__(self, items) -> None:
        self.items = items

    def all(self):
        return self.items


def _bound(statement, key: str):
    return statement.compile().params.get(key)


def test_enqueue_background_job_persists_queue_id(monkeypatch) -> None:
    db = FakeDB()
    monkeypatch.setattr(
        "backend.worker.tasks.dispatch_task",
        lambda task_name, job_id, payload, **kwargs: type("AsyncResult", (), {"id": kwargs.get("queue_id") or f"queue-{job_id}"})(),
    )

    with bind_trace_context(trace_id="trace-jobs", request_id="req-jobs"):
        job = enqueue_background_job(db, task_name="dream_review", payload={"limit": 1}, triggered_by="test")

    assert isinstance(job, BackgroundJob)
    assert job.status == "queued"
    assert job.queue_id
    assert job.payload == {"limit": 1}
    assert job.trace_id == "trace-jobs"
    assert job.request_id == "req-jobs"
    assert any(isinstance(item, IdempotencyRecord) and item.idempotency_key == job.idempotency_key for item in db.objects)
    outbox = next(item for item in db.objects if isinstance(item, OutboxMessage))
    assert outbox.status == "dispatched"
    assert outbox.trace_id == "trace-jobs"
    assert outbox.payload["job_id"] == str(job.id)
    assert db.commits == 0


def test_enqueue_background_job_reuses_inflight_idempotency_key(monkeypatch) -> None:
    db = FakeDB()
    monkeypatch.setattr(
        "backend.worker.tasks.dispatch_task",
        lambda task_name, job_id, payload, **kwargs: type("AsyncResult", (), {"id": kwargs.get("queue_id") or f"queue-{job_id}"})(),
    )

    first = enqueue_background_job(db, task_name="dream_review", payload={"limit": 1}, triggered_by="test", idempotency_key="same")
    second = enqueue_background_job(db, task_name="dream_review", payload={"limit": 1}, triggered_by="test", idempotency_key="same")

    assert second is first
    assert len([item for item in db.objects if isinstance(item, BackgroundJob)]) == 1


def test_background_job_status_transitions() -> None:
    db = FakeDB()
    service = BackgroundJobService()
    job = service.create(db, task_name="wiki_lint")

    service.mark_started(db, job.id)
    service.mark_succeeded(db, job.id, {"ok": True})

    assert job.status == "succeeded"
    assert job.result == {"ok": True}
