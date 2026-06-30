"""Dry-run reliability probe for local engineering checks."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from backend.infra.config import get_settings
from backend.infra.db import session_scope
from backend.infra.models import BackgroundJob, CronJob, OutboxMessage
from backend.infra.rate_limit import FixedWindowRateLimiter, RateLimitExceeded
from backend.infra.resilience import ResilienceManager, ResiliencePolicy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run safe SoulClaw reliability probes.")
    parser.add_argument("--scenario", choices=["all", "api", "llm", "outbox", "cron", "jobs"], default="all")
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args(argv)
    report = run_probe(scenario=args.scenario, requests=args.requests, concurrency=args.concurrency)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


def run_probe(*, scenario: str = "all", requests: int = 20, concurrency: int = 4) -> dict[str, Any]:
    scenarios = ["api", "llm", "outbox", "cron", "jobs"] if scenario == "all" else [scenario]
    started = time.perf_counter()
    result: dict[str, Any] = {"scenario": scenario, "dry_run": True, "items": {}}
    if "api" in scenarios:
        result["items"]["api"] = _probe_api(requests=requests, concurrency=concurrency)
    if "llm" in scenarios:
        result["items"]["llm"] = _probe_llm_failure()
    if "outbox" in scenarios:
        result["items"]["outbox"] = _probe_outbox()
    if "cron" in scenarios:
        result["items"]["cron"] = _probe_cron()
    if "jobs" in scenarios:
        result["items"]["jobs"] = _probe_jobs()
    result["duration_seconds"] = round(time.perf_counter() - started, 4)
    return result


def _probe_api(*, requests: int, concurrency: int) -> dict[str, Any]:
    limiter = FixedWindowRateLimiter(settings=get_settings())
    latencies: list[float] = []
    blocked = 0
    total = max(1, int(requests or 1))

    def one(index: int) -> tuple[float, bool]:
        start = time.perf_counter()
        try:
            limiter.enforce(policy="probe_api", identity="dry-run", limit=max(1, total // 2))
            allowed = True
        except RateLimitExceeded:
            allowed = False
        return time.perf_counter() - start, allowed

    with ThreadPoolExecutor(max_workers=max(1, int(concurrency or 1))) as pool:
        futures = [pool.submit(one, index) for index in range(total)]
        for future in as_completed(futures):
            latency, allowed = future.result()
            latencies.append(latency)
            blocked += 0 if allowed else 1
    return {
        "requests": total,
        "blocked": blocked,
        "error_rate": round(blocked / total, 4),
        "p95_ms": round(_p95(latencies) * 1000, 3),
        "rate_limit": limiter.state(),
    }


def _probe_llm_failure() -> dict[str, Any]:
    manager = ResilienceManager()
    policy = ResiliencePolicy(name="probe.llm", max_attempts=1, failure_threshold=1, recovery_seconds=30)
    try:
        manager.call(policy, lambda: (_ for _ in ()).throw(TimeoutError("synthetic timeout")))
    except TimeoutError:
        pass
    return {"circuit": manager.state().get("probe.llm", {}), "failure_injected": True}


def _probe_outbox() -> dict[str, Any]:
    try:
        with session_scope() as db:
            pending = _count(db, OutboxMessage, ["pending", "retrying"])
            dead = _count(db, OutboxMessage, ["dead_letter"])
    except SQLAlchemyError as exc:
        return {"available": False, "error": str(exc)}
    return {"pending_or_retrying": pending, "dead_letter": dead}


def _probe_cron() -> dict[str, Any]:
    try:
        with session_scope() as db:
            backoff = int(db.scalar(select(func.count()).select_from(CronJob).where(CronJob.backoff_until.is_not(None))) or 0)
    except SQLAlchemyError as exc:
        return {"available": False, "error": str(exc)}
    return {"backoff": backoff}


def _probe_jobs() -> dict[str, Any]:
    try:
        with session_scope() as db:
            retrying = _count(db, BackgroundJob, ["retrying"])
            dead = _count(db, BackgroundJob, ["dead_letter"])
    except SQLAlchemyError as exc:
        return {"available": False, "error": str(exc)}
    return {"retrying": retrying, "dead_letter": dead}


def _count(db, model, statuses: list[str]) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(model.status.in_(statuses))) or 0)


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    if len(values) < 20:
        return max(values)
    return statistics.quantiles(values, n=20)[18]


if __name__ == "__main__":
    raise SystemExit(main())
