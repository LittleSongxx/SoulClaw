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
    def __init__(self) -> None:
        self.calls = []

    def run_turn(self, db, message: str, *, session_id: str = "local"):
        self.calls.append({"message": message, "session_id": session_id})
        return type("Result", (), {"turn_id": "turn-1", "answer": f"ran: {message}", "context": {}})()


class DummyDream:
    def __init__(self) -> None:
        self.calls = []

    def run_review(self, db, *, window_hours: int = 24, limit: int = 50):
        self.calls.append({"window_hours": window_hours, "limit": limit})
        return type(
            "Result",
            (),
            {
                "scanned_memories": 2,
                "failed_tool_runs": 1,
                "proposals_created": 1,
                "proposal_ids": ["proposal-1"],
            },
        )()


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
        self.metadata_json = {}


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


def test_cron_scheduler_runs_dream_review_job_without_agent_turn() -> None:
    agent = DummyAgent()
    dream = DummyDream()
    scheduler = CronScheduler(agent=agent, dream=dream, events=DummyEvents())
    job = DummyCronJob()
    job.name = "system-dream-review"
    job.metadata_json = {"system_task": "dream_review", "window_hours": 48, "limit": 7}
    now = datetime(2026, 6, 25, 3, 30, tzinfo=UTC)

    scheduler._run_job(None, job, now=now)

    assert agent.calls == []
    assert dream.calls == [{"window_hours": 48, "limit": 7}]
    assert job.last_status == "succeeded"
    assert job.last_result["mode"] == "dream_review"
    assert job.last_result["proposals_created"] == 1
