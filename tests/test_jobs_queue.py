from __future__ import annotations

import uuid

from backend.domain.jobs import BackgroundJobService, enqueue_background_job
from backend.infra.models import BackgroundJob


class FakeDB:
    def __init__(self) -> None:
        self.objects = []

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


def test_enqueue_background_job_persists_queue_id(monkeypatch) -> None:
    db = FakeDB()
    monkeypatch.setattr(
        "backend.worker.tasks.dispatch_task",
        lambda task_name, job_id, payload, **kwargs: type("AsyncResult", (), {"id": kwargs.get("queue_id") or f"queue-{job_id}"})(),
    )

    job = enqueue_background_job(db, task_name="dream_review", payload={"limit": 1}, triggered_by="test")

    assert isinstance(job, BackgroundJob)
    assert job.status == "queued"
    assert job.queue_id
    assert job.payload == {"limit": 1}


def test_background_job_status_transitions() -> None:
    db = FakeDB()
    service = BackgroundJobService()
    job = service.create(db, task_name="wiki_lint")

    service.mark_started(db, job.id)
    service.mark_succeeded(db, job.id, {"ok": True})

    assert job.status == "succeeded"
    assert job.result == {"ok": True}
