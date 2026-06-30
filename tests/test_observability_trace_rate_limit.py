from __future__ import annotations

import uuid

import pytest

from backend.cli.reliability_probe import run_probe
from backend.infra.config import Settings
from backend.infra.events import RuntimeEventBus
from backend.infra.models import BackgroundJob, CronJob, OutboxMessage, RuntimeEvent
from backend.infra.observability import Observability, reliability_alerts
from backend.infra.rate_limit import FixedWindowRateLimiter, RateLimitExceeded
from backend.infra.trace import bind_trace_context, trace_id_from_traceparent


class FakeDB:
    def __init__(self, rows=None, scalar_value=0) -> None:
        self.rows = rows or []
        self.scalar_value = scalar_value

    def execute(self, statement):
        del statement
        return type("Result", (), {"all": lambda _self: self.rows})()

    def scalar(self, statement):
        del statement
        return self.scalar_value


def test_trace_context_parses_traceparent_and_events_capture_context() -> None:
    trace_id = "0123456789abcdef0123456789abcdef"
    assert trace_id_from_traceparent(f"00-{trace_id}-0123456789abcdef-01") == trace_id
    db = type("DB", (), {"objects": [], "add": lambda self, item: self.objects.append(item)})()
    bus = RuntimeEventBus()

    with bind_trace_context(trace_id=trace_id, request_id="req-1"):
        with bus.bind_session(db):
            bus.emit("trace.test", {"ok": True})
            bus.audit("trace.audit", "thing")

    assert any(isinstance(item, RuntimeEvent) and item.trace_id == trace_id for item in db.objects)
    assert {getattr(item, "request_id", "") for item in db.objects} == {"req-1"}


def test_rate_limiter_blocks_after_fixed_window_limit() -> None:
    settings = Settings(rate_limit_enabled=True, rate_limit_memory_fallback=True)
    limiter = FixedWindowRateLimiter(settings=settings)

    first = limiter.enforce(policy="turn", identity="user-1", limit=1, window_seconds=60)

    assert first.allowed is True
    with pytest.raises(RateLimitExceeded) as exc:
        limiter.enforce(policy="turn", identity="user-1", limit=1, window_seconds=60)
    assert exc.value.retry_after > 0
    assert limiter.state()["blocked"]["turn"] == 1


def test_observability_exports_metrics() -> None:
    obs = Observability(settings=Settings())
    obs.record_http(method="GET", path="/api/test", status_code=200, duration_seconds=0.01)
    obs.record_external_call(subsystem="llm", operation="openai", status="succeeded")
    obs.collect_reliability(FakeDB(rows=[("queued", 2)], scalar_value=0), resilience_state={}, rate_limit_state={})

    payload = obs.metrics_response().body.decode()

    assert "soulclaw_http_requests_total" in payload
    assert "soulclaw_external_calls_total" in payload
    assert "soulclaw_background_jobs" in payload


def test_reliability_alerts_surface_dead_letter_and_circuit() -> None:
    alerts = reliability_alerts(
        FakeDB(scalar_value=1),
        settings=Settings(),
        redis_client=None,
        resilience_state={"llm.openai": {"opened_until": "soon", "last_error": "timeout"}},
    )

    kinds = {item["kind"] for item in alerts}
    assert {"dead_letter", "outbox_backlog", "cron_backoff", "circuit_open"}.issubset(kinds)


def test_reliability_probe_dry_run_api_and_llm() -> None:
    report = run_probe(scenario="all", requests=4, concurrency=2)

    assert report["dry_run"] is True
    assert "api" in report["items"]
    assert "llm" in report["items"]
    assert "duration_seconds" in report


def test_trace_columns_exist_on_runtime_models() -> None:
    job = BackgroundJob(id=uuid.uuid4(), task_name="wiki_lint", trace_id="trace", request_id="req")
    outbox = OutboxMessage(id=uuid.uuid4(), topic="t", aggregate_type="job", aggregate_id="1", trace_id="trace", request_id="req")
    event = RuntimeEvent(id=uuid.uuid4(), event_type="e", trace_id="trace", request_id="req")
    cron = CronJob(id=uuid.uuid4(), name="c", cron_expr="* * * * *")

    assert job.trace_id == outbox.trace_id == event.trace_id == "trace"
    assert cron.name == "c"
