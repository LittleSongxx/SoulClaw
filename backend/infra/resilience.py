"""Lightweight retry, timeout, and circuit-breaker primitives."""

from __future__ import annotations

import random
import time
import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import httpx

from backend.infra.events import RuntimeEventBus

T = TypeVar("T")


class CircuitOpenError(RuntimeError):
    pass


@dataclass
class ResiliencePolicy:
    name: str
    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 4.0
    jitter_ratio: float = 0.2
    failure_threshold: int = 5
    recovery_seconds: float = 30.0
    retry_status_codes: set[int] = field(default_factory=lambda: {408, 409, 425, 429, 500, 502, 503, 504})


@dataclass
class CircuitState:
    failures: int = 0
    opened_until: datetime | None = None
    last_error: str = ""


class ResilienceManager:
    def __init__(self, *, events: RuntimeEventBus | None = None, observability: Any | None = None) -> None:
        self.events = events
        self.observability = observability
        self._states: dict[str, CircuitState] = {}

    def call(self, policy: ResiliencePolicy, fn: Callable[[], T]) -> T:
        state = self._states.setdefault(policy.name, CircuitState())
        now = datetime.now(UTC)
        if state.opened_until and state.opened_until > now:
            if self.events:
                self.events.emit(
                    "resilience.circuit_open",
                    {"name": policy.name, "opened_until": state.opened_until.isoformat(), "last_error": state.last_error},
                    severity="warning",
                )
            self._record_observability(policy, "circuit_open")
            raise CircuitOpenError(f"circuit open for {policy.name} until {state.opened_until.isoformat()}")
        if state.opened_until and state.opened_until <= now:
            state.opened_until = None

        last_exc: Exception | None = None
        for attempt in range(1, max(1, policy.max_attempts) + 1):
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if not should_retry(exc, policy) or attempt >= policy.max_attempts:
                    self._record_failure(policy, state, exc)
                    raise
                delay = retry_delay(policy, attempt, exc)
                if self.events:
                    self.events.emit(
                        "resilience.retry",
                        {"name": policy.name, "attempt": attempt, "delay_seconds": delay, "error": str(exc)},
                        severity="warning",
                    )
                time.sleep(delay)
                continue
            state.failures = 0
            state.opened_until = None
            state.last_error = ""
            self._record_observability(policy, "succeeded")
            return result
        assert last_exc is not None
        self._record_failure(policy, state, last_exc)
        raise last_exc

    async def async_call(self, policy: ResiliencePolicy, fn: Callable[[], Any]) -> T:
        state = self._states.setdefault(policy.name, CircuitState())
        now = datetime.now(UTC)
        if state.opened_until and state.opened_until > now:
            if self.events:
                self.events.emit(
                    "resilience.circuit_open",
                    {"name": policy.name, "opened_until": state.opened_until.isoformat(), "last_error": state.last_error},
                    severity="warning",
                )
            self._record_observability(policy, "circuit_open")
            raise CircuitOpenError(f"circuit open for {policy.name} until {state.opened_until.isoformat()}")
        if state.opened_until and state.opened_until <= now:
            state.opened_until = None

        last_exc: Exception | None = None
        for attempt in range(1, max(1, policy.max_attempts) + 1):
            try:
                result = await fn()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if not should_retry(exc, policy) or attempt >= policy.max_attempts:
                    self._record_failure(policy, state, exc)
                    raise
                delay = retry_delay(policy, attempt, exc)
                if self.events:
                    self.events.emit(
                        "resilience.retry",
                        {"name": policy.name, "attempt": attempt, "delay_seconds": delay, "error": str(exc)},
                        severity="warning",
                    )
                await asyncio.sleep(delay)
                continue
            state.failures = 0
            state.opened_until = None
            state.last_error = ""
            self._record_observability(policy, "succeeded")
            return result
        assert last_exc is not None
        self._record_failure(policy, state, last_exc)
        raise last_exc

    def state(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "failures": state.failures,
                "opened_until": state.opened_until.isoformat() if state.opened_until else None,
                "last_error": state.last_error,
            }
            for name, state in sorted(self._states.items())
        }

    def _record_failure(self, policy: ResiliencePolicy, state: CircuitState, exc: Exception) -> None:
        state.failures += 1
        state.last_error = str(exc)
        if state.failures >= max(1, policy.failure_threshold):
            state.opened_until = datetime.now(UTC) + timedelta(seconds=max(1.0, policy.recovery_seconds))
            if self.events:
                self.events.emit(
                    "resilience.circuit_tripped",
                    {"name": policy.name, "failures": state.failures, "error": str(exc)},
                    severity="error",
                )
        self._record_observability(policy, "failed")

    def _record_observability(self, policy: ResiliencePolicy, status: str) -> None:
        if self.observability is None:
            return
        name_parts = policy.name.split(".", 1)
        subsystem = name_parts[0] if name_parts else policy.name
        operation = name_parts[1] if len(name_parts) > 1 else policy.name
        recorder = getattr(self.observability, "record_external_call", None)
        if recorder is not None:
            recorder(subsystem=subsystem, operation=operation, status=status)


def should_retry(exc: Exception, policy: ResiliencePolicy) -> bool:
    if isinstance(exc, CircuitOpenError):
        return False
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError, TimeoutError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return int(exc.response.status_code) in policy.retry_status_codes
    return False


def retry_delay(policy: ResiliencePolicy, attempt: int, exc: Exception) -> float:
    retry_after = _retry_after_seconds(exc)
    if retry_after is not None:
        return min(policy.max_delay_seconds, retry_after)
    base = min(policy.max_delay_seconds, policy.base_delay_seconds * (2 ** max(0, attempt - 1)))
    jitter = base * max(0.0, policy.jitter_ratio)
    return max(0.0, base + random.uniform(-jitter, jitter))


def _retry_after_seconds(exc: Exception) -> float | None:
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    value = exc.response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
