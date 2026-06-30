from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app import RateLimitMiddleware, TraceContextMiddleware
from backend.infra.config import Settings
from backend.infra.observability import Observability, ObservabilityMiddleware
from backend.infra.rate_limit import FixedWindowRateLimiter


def _app(settings: Settings) -> FastAPI:
    app = FastAPI()
    observability = Observability(settings=settings)
    app.state.settings = settings
    app.state.observability = observability
    app.state.rate_limiter = FixedWindowRateLimiter(settings=settings)
    app.add_middleware(ObservabilityMiddleware, observability=observability)
    app.add_middleware(TraceContextMiddleware)
    app.add_middleware(RateLimitMiddleware)

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.post("/api/runs/turn")
    def turn():
        return {"ok": True}

    @app.get("/metrics")
    def metrics():
        return observability.metrics_response()

    return app


def test_trace_middleware_returns_request_headers() -> None:
    client = TestClient(_app(Settings(rate_limit_enabled=False)))

    response = client.get("/api/health", headers={"X-Request-ID": "req-test"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req-test"
    assert response.headers["traceparent"].startswith("00-")


def test_rate_limit_middleware_blocks_turn_requests_and_metrics_are_exempt() -> None:
    settings = Settings(rate_limit_enabled=True, rate_limit_turn_per_minute=1, rate_limit_memory_fallback=True)
    client = TestClient(_app(settings))

    assert client.post("/api/runs/turn").status_code == 200
    blocked = client.post("/api/runs/turn")
    metrics = client.get("/metrics")

    assert blocked.status_code == 429
    assert blocked.json()["code"] == "rate_limit_exceeded"
    assert "Retry-After" in blocked.headers
    assert metrics.status_code == 200
    assert "soulclaw_http_requests_total" in metrics.text
