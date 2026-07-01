"""Prometheus metrics, OpenTelemetry setup, and reliability summaries."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from fastapi import Request, Response
from loguru import logger
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from sqlalchemy import func, select
from starlette.middleware.base import BaseHTTPMiddleware

from backend.infra.config import Settings
from backend.infra.health import readiness_summary
from backend.infra.models import BackgroundJob, CronJob, IdempotencyRecord, OutboxMessage


class Observability:
    def __init__(self, *, settings: Settings) -> None:
        self.settings = settings
        self.registry = CollectorRegistry(auto_describe=True)
        self.http_requests = Counter(
            "soulclaw_http_requests_total",
            "HTTP requests by method, path template, and status.",
            ["method", "path", "status"],
            registry=self.registry,
        )
        self.http_latency = Histogram(
            "soulclaw_http_request_duration_seconds",
            "HTTP request latency.",
            ["method", "path"],
            registry=self.registry,
        )
        self.external_calls = Counter(
            "soulclaw_external_calls_total",
            "External runtime calls by subsystem and status.",
            ["subsystem", "operation", "status"],
            registry=self.registry,
        )
        self.rate_limit_hits = Counter(
            "soulclaw_rate_limit_hits_total",
            "Rate limit decisions by policy and status.",
            ["policy", "status"],
            registry=self.registry,
        )
        self.jobs = Gauge(
            "soulclaw_background_jobs",
            "Background jobs by status.",
            ["status"],
            registry=self.registry,
        )
        self.outbox = Gauge(
            "soulclaw_outbox_messages",
            "Outbox messages by status.",
            ["status"],
            registry=self.registry,
        )
        self.idempotency = Gauge(
            "soulclaw_idempotency_records",
            "Idempotency records by status.",
            ["status"],
            registry=self.registry,
        )
        self.cron_backoff = Gauge(
            "soulclaw_cron_backoff_jobs",
            "Cron jobs currently in backoff.",
            registry=self.registry,
        )
        self.circuit_open = Gauge(
            "soulclaw_resilience_circuit_open",
            "Circuit breaker open state by name.",
            ["name"],
            registry=self.registry,
        )

    def record_http(self, *, method: str, path: str, status_code: int, duration_seconds: float) -> None:
        self.http_requests.labels(method=method, path=path, status=str(status_code)).inc()
        self.http_latency.labels(method=method, path=path).observe(max(0.0, duration_seconds))

    def record_external_call(self, *, subsystem: str, operation: str, status: str) -> None:
        self.external_calls.labels(subsystem=subsystem, operation=operation, status=status).inc()

    def record_rate_limit(self, *, policy: str, allowed: bool) -> None:
        self.rate_limit_hits.labels(policy=policy, status="allowed" if allowed else "blocked").inc()

    def collect_reliability(self, db, *, resilience_state: dict[str, Any], rate_limit_state: dict[str, Any]) -> dict[str, Any]:
        jobs = _status_counts(db, BackgroundJob.status)
        outbox = _status_counts(db, OutboxMessage.status)
        idempotency = _status_counts(db, IdempotencyRecord.status)
        for status, count in jobs.items():
            self.jobs.labels(status=status).set(count)
        for status, count in outbox.items():
            self.outbox.labels(status=status).set(count)
        for status, count in idempotency.items():
            self.idempotency.labels(status=status).set(count)
        backoff = int(db.scalar(select(func.count()).select_from(CronJob).where(CronJob.backoff_until.is_not(None))) or 0)
        self.cron_backoff.set(backoff)
        for name, state in resilience_state.items():
            self.circuit_open.labels(name=name).set(1 if state.get("opened_until") else 0)
        for policy, count in (rate_limit_state.get("hits") or {}).items():
            blocked = int((rate_limit_state.get("blocked") or {}).get(policy) or 0)
            allowed = max(0, int(count or 0) - blocked)
            if allowed:
                self.rate_limit_hits.labels(policy=policy, status="allowed").inc(0)
            if blocked:
                self.rate_limit_hits.labels(policy=policy, status="blocked").inc(0)
        return {"jobs": jobs, "outbox": outbox, "idempotency": idempotency, "cron_backoff": backoff}

    def metrics_response(self) -> Response:
        return Response(generate_latest(self.registry), media_type=CONTENT_TYPE_LATEST)


class ObservabilityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, observability: Observability) -> None:
        super().__init__(app)
        self.observability = observability

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start = time.perf_counter()
        response: Response | None = None
        try:
            response = await call_next(request)
            return response
        finally:
            if response is not None:
                self.observability.record_http(
                    method=request.method,
                    path=_path_label(request),
                    status_code=response.status_code,
                    duration_seconds=time.perf_counter() - start,
                )


def setup_opentelemetry(app, *, settings: Settings, engine=None) -> dict[str, Any]:
    if not settings.tracing_enabled:
        return {"enabled": False, "reason": "disabled"}
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    except Exception as exc:  # noqa: BLE001
        logger.info("[otel] instrumentation unavailable: {}", exc)
        return {"enabled": False, "reason": "dependencies_unavailable"}

    provider = TracerProvider(resource=Resource.create({"service.name": settings.app_name}))
    if settings.otel_exporter_otlp_endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)))
    else:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()
    if engine is not None:
        SQLAlchemyInstrumentor().instrument(engine=engine)
    return {"enabled": True, "exporter": "otlp" if settings.otel_exporter_otlp_endpoint else "console"}


def reliability_alerts(
    db,
    *,
    settings: Settings,
    redis_client: Any | None,
    resilience_state: dict[str, Any],
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    readiness = readiness_summary(settings, redis_client)
    if not readiness.get("ok"):
        alerts.append({"severity": "critical", "kind": "readiness", "message": "readiness check failed", "details": readiness.get("checks", {})})
    dead_letters = int(db.scalar(select(func.count()).select_from(BackgroundJob).where(BackgroundJob.status == "dead_letter")) or 0)
    if dead_letters:
        alerts.append({"severity": "critical", "kind": "dead_letter", "message": f"{dead_letters} background jobs are in dead letter", "count": dead_letters})
    outbox_pending = int(db.scalar(select(func.count()).select_from(OutboxMessage).where(OutboxMessage.status.in_(["pending", "retrying"]))) or 0)
    if outbox_pending:
        alerts.append({"severity": "warning", "kind": "outbox_backlog", "message": f"{outbox_pending} outbox messages await dispatch", "count": outbox_pending})
    cron_backoff = int(db.scalar(select(func.count()).select_from(CronJob).where(CronJob.backoff_until.is_not(None))) or 0)
    if cron_backoff:
        alerts.append({"severity": "warning", "kind": "cron_backoff", "message": f"{cron_backoff} cron jobs are in backoff", "count": cron_backoff})
    for name, state in sorted(resilience_state.items()):
        if state.get("opened_until"):
            alerts.append({"severity": "critical", "kind": "circuit_open", "message": f"circuit {name} is open", "name": name, "details": state})
    return alerts


def _status_counts(db, column) -> dict[str, int]:
    rows = db.execute(select(column, func.count()).group_by(column)).all()
    return {str(status or "unknown"): int(count or 0) for status, count in rows}


def _path_label(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path or request.url.path)
