"""Fixed-window rate limiting with Redis first and in-memory fallback."""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from typing import Any

from backend.infra.config import Settings, get_settings
from backend.infra.events import RuntimeEventBus


class RateLimitExceeded(RuntimeError):
    def __init__(self, *, key: str, policy: str, limit: int, retry_after: int) -> None:
        super().__init__(f"rate limit exceeded for {policy}")
        self.key = key
        self.policy = policy
        self.limit = limit
        self.retry_after = retry_after


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    policy: str
    key: str
    limit: int
    remaining: int
    retry_after: int
    reset_at: int
    backend: str


class FixedWindowRateLimiter:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        redis_client: Any | None = None,
        events: RuntimeEventBus | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.redis = redis_client
        self.events = events
        self._memory: dict[str, tuple[int, int]] = {}
        self._lock = Lock()
        self._hits_by_policy: dict[str, int] = {}
        self._blocked_by_policy: dict[str, int] = {}

    def check(self, *, policy: str, identity: str, limit: int, window_seconds: int = 60) -> RateLimitDecision:
        if not self.settings.rate_limit_enabled:
            now = int(time.time())
            return RateLimitDecision(True, policy, identity, limit, limit, 0, now + window_seconds, "disabled")
        limit = max(1, int(limit or 1))
        identity = _safe_identity(identity)
        key = f"soulclaw:rl:{policy}:{identity}"
        if self.redis is not None:
            try:
                decision = self._check_redis(
                    key=key,
                    policy=policy,
                    limit=limit,
                    window_seconds=window_seconds,
                )
                self._record(decision)
                return decision
            except Exception as exc:  # noqa: BLE001
                if self.events:
                    self.events.emit("rate_limit.redis_unavailable", {"policy": policy, "error": str(exc)}, severity="warning")
                if not self.settings.rate_limit_memory_fallback:
                    now = int(time.time())
                    decision = RateLimitDecision(True, policy, key, limit, limit, 0, now + window_seconds, "redis_unavailable_open")
                    self._record(decision)
                    return decision
        decision = self._check_memory(key=key, policy=policy, limit=limit, window_seconds=window_seconds)
        self._record(decision)
        return decision

    def enforce(self, *, policy: str, identity: str, limit: int, window_seconds: int = 60) -> RateLimitDecision:
        decision = self.check(policy=policy, identity=identity, limit=limit, window_seconds=window_seconds)
        if not decision.allowed:
            if self.events:
                self.events.emit(
                    "rate_limit.blocked",
                    {"policy": policy, "key": decision.key, "limit": limit, "retry_after": decision.retry_after},
                    severity="warning",
                )
            raise RateLimitExceeded(key=decision.key, policy=policy, limit=limit, retry_after=decision.retry_after)
        return decision

    def state(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.rate_limit_enabled,
            "backend": "redis" if self.redis is not None else ("memory" if self.settings.rate_limit_memory_fallback else "none"),
            "hits": dict(sorted(self._hits_by_policy.items())),
            "blocked": dict(sorted(self._blocked_by_policy.items())),
            "policies": {
                "admin": {"limit": self.settings.rate_limit_admin_per_minute, "window_seconds": 60},
                "turn": {"limit": self.settings.rate_limit_turn_per_minute, "window_seconds": 60},
                "gateway": {"limit": self.settings.rate_limit_gateway_per_minute, "window_seconds": 60},
                "llm": {"limit": self.settings.rate_limit_llm_per_minute, "window_seconds": 60},
            },
        }

    def _check_redis(self, *, key: str, policy: str, limit: int, window_seconds: int) -> RateLimitDecision:
        now = int(time.time())
        count = int(self.redis.incr(key))
        if count == 1:
            self.redis.expire(key, window_seconds)
        ttl = int(self.redis.ttl(key) or window_seconds)
        if ttl < 0:
            ttl = window_seconds
        allowed = count <= limit
        remaining = max(0, limit - count)
        return RateLimitDecision(allowed, policy, key, limit, remaining, ttl if not allowed else 0, now + ttl, "redis")

    def _check_memory(self, *, key: str, policy: str, limit: int, window_seconds: int) -> RateLimitDecision:
        now = int(time.time())
        window_start = now - (now % window_seconds)
        reset_at = window_start + window_seconds
        with self._lock:
            count, stored_reset = self._memory.get(key, (0, reset_at))
            if stored_reset <= now:
                count = 0
                stored_reset = reset_at
            count += 1
            self._memory[key] = (count, stored_reset)
        allowed = count <= limit
        remaining = max(0, limit - count)
        retry_after = max(1, stored_reset - now) if not allowed else 0
        return RateLimitDecision(allowed, policy, key, limit, remaining, retry_after, stored_reset, "memory")

    def _record(self, decision: RateLimitDecision) -> None:
        self._hits_by_policy[decision.policy] = self._hits_by_policy.get(decision.policy, 0) + 1
        if not decision.allowed:
            self._blocked_by_policy[decision.policy] = self._blocked_by_policy.get(decision.policy, 0) + 1


def _safe_identity(identity: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "_.:-" else "_" for ch in str(identity or "anonymous"))[:160]
