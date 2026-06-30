from __future__ import annotations

import httpx
import pytest

from backend.infra.resilience import CircuitOpenError, ResilienceManager, ResiliencePolicy


def test_resilience_retries_retryable_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backend.infra.resilience.time.sleep", lambda delay: None)
    manager = ResilienceManager()
    policy = ResiliencePolicy(name="test.retry", max_attempts=3, base_delay_seconds=0, jitter_ratio=0)
    calls = {"count": 0}
    request = httpx.Request("GET", "https://example.test")
    response = httpx.Response(503, request=request)

    def flaky():
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.HTTPStatusError("busy", request=request, response=response)
        return "ok"

    assert manager.call(policy, flaky) == "ok"
    assert calls["count"] == 2


def test_resilience_does_not_retry_non_retryable_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backend.infra.resilience.time.sleep", lambda delay: None)
    manager = ResilienceManager()
    policy = ResiliencePolicy(name="test.no_retry", max_attempts=3, base_delay_seconds=0, jitter_ratio=0)
    calls = {"count": 0}
    request = httpx.Request("GET", "https://example.test")
    response = httpx.Response(400, request=request)

    def bad_request():
        calls["count"] += 1
        raise httpx.HTTPStatusError("bad", request=request, response=response)

    with pytest.raises(httpx.HTTPStatusError):
        manager.call(policy, bad_request)
    assert calls["count"] == 1


def test_resilience_opens_circuit_after_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backend.infra.resilience.time.sleep", lambda delay: None)
    manager = ResilienceManager()
    policy = ResiliencePolicy(
        name="test.circuit",
        max_attempts=1,
        failure_threshold=2,
        recovery_seconds=60,
    )

    def timeout():
        raise httpx.TimeoutException("timeout")

    with pytest.raises(httpx.TimeoutException):
        manager.call(policy, timeout)
    with pytest.raises(httpx.TimeoutException):
        manager.call(policy, timeout)
    with pytest.raises(CircuitOpenError):
        manager.call(policy, lambda: "not reached")
    assert manager.state()["test.circuit"]["opened_until"] is not None
