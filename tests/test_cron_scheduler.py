from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from backend.domain.cron_schedule import compute_next_run
from backend.runtime.cron import CronScheduler


class DummyEvents:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event_type, payload=None, **kwargs) -> None:
        self.events.append((event_type, payload or {}, kwargs))


class DummyAgent:
    def run_turn(self, db, message: str, *, session_id: str = "local"):
        return type("Result", (), {"turn_id": "turn-1", "answer": f"ran: {message}", "context": {}})()


class DummyCronJob:
    def __init__(self) -> None:
        self.id = uuid4()
        self.name = "daily"
        self.cron_expr = "0 3 * * *"
        self.timezone = "UTC"
        self.instruction = "hello"
        self.last_status = "never_run"
        self.last_result = {}
        self.last_run_at = None
        self.next_run_at = None
        self.run_count = 0
        self.failure_count = 0


def test_compute_next_run_returns_utc_datetime() -> None:
    base = datetime(2026, 6, 25, 2, 0, tzinfo=UTC)

    next_run = compute_next_run("0 3 * * *", "UTC", base=base)

    assert next_run == datetime(2026, 6, 25, 3, 0, tzinfo=UTC)


def test_cron_scheduler_run_job_updates_state() -> None:
    scheduler = CronScheduler(agent=DummyAgent(), events=DummyEvents())
    job = DummyCronJob()
    now = datetime(2026, 6, 25, 3, 0, tzinfo=UTC)

    scheduler._run_job(None, job, now=now)

    assert job.last_status == "succeeded"
    assert job.last_result["answer"] == "ran: hello"
    assert job.last_run_at == now
    assert job.next_run_at == datetime(2026, 6, 26, 3, 0, tzinfo=UTC)
    assert job.run_count == 1

