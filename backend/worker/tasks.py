"""Celery task entrypoints."""

from __future__ import annotations

import asyncio
import socket
import uuid
from typing import Any

from backend.infra.db import session_scope
from backend.infra.trace import bind_trace_context
from backend.worker.bootstrap import build_worker_services
from backend.worker.celery_app import celery_app


def dispatch_task(
    task_name: str,
    job_id: str,
    payload: dict[str, Any],
    *,
    queue_id: str | None = None,
    trace_id: str = "",
    request_id: str = "",
):
    tasks = {
        "dream_review": dream_review_task,
        "wiki_compile": wiki_compile_task,
        "wiki_lint": wiki_lint_task,
        "wiki_repair": wiki_repair_task,
        "skill_scan": skill_scan_task,
        "embedding_rebuild": embedding_rebuild_task,
        "mcp_refresh": mcp_refresh_task,
        "heartbeat_check": heartbeat_check_task,
        "a2a_sync": a2a_sync_task,
    }
    task = tasks.get(task_name)
    if task is None:
        raise ValueError(f"unsupported background task: {task_name}")
    task_payload = dict(payload or {})
    if trace_id:
        task_payload.setdefault("_trace_id", trace_id)
    if request_id:
        task_payload.setdefault("_request_id", request_id)
    if queue_id and hasattr(task, "apply_async"):
        return task.apply_async(args=(job_id, task_payload), task_id=queue_id)
    return task.delay(job_id, task_payload)


def _run_job(job_id: str, fn):
    services = build_worker_services()
    parsed_id = uuid.UUID(str(job_id))
    lock_owner = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
    trace_id = ""
    request_id = ""
    with session_scope() as db:
        with services.events.bind_session(db):
            job = services.jobs.get(db, parsed_id)
            if job is None:
                raise KeyError(f"background job not found: {parsed_id}")
            trace_id = str(getattr(job, "trace_id", "") or "")
            request_id = str(getattr(job, "request_id", "") or "")
            if not services.jobs.should_run(job):
                return {
                    "status": "skipped",
                    "job_status": job.status,
                    "reason": "not runnable or waiting for retry window",
                }
            job = services.jobs.mark_started(db, parsed_id, lock_owner=lock_owner)
            attempt = int(job.attempt_count or 0)
            max_attempts = int(job.max_attempts or 3)
    with bind_trace_context(trace_id=trace_id, request_id=request_id):
        try:
            with session_scope() as db:
                with services.events.bind_session(db):
                    result = fn(services, db)
        except Exception as exc:  # noqa: BLE001
            with session_scope() as db:
                with services.events.bind_session(db):
                    if attempt >= max_attempts:
                        services.jobs.mark_dead_letter(db, parsed_id, str(exc))
                    else:
                        services.jobs.mark_retrying(db, parsed_id, str(exc))
            raise
        with session_scope() as db:
            with services.events.bind_session(db):
                services.jobs.mark_succeeded(db, parsed_id, result)
        return result


@celery_app.task(name="soulclaw.dream_review")
def dream_review_task(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    def run(services, db):
        result = services.dream.run_review(
            db,
            window_hours=int(payload.get("window_hours") or 24),
            limit=int(payload.get("limit") or 50),
        )
        return {
            "scanned_memories": result.scanned_memories,
            "failed_tool_runs": result.failed_tool_runs,
            "proposals_created": result.proposals_created,
            "proposal_ids": result.proposal_ids,
        }

    return _run_job(job_id, run)


@celery_app.task(name="soulclaw.wiki_compile")
def wiki_compile_task(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    del payload
    return _run_job(job_id, lambda services, db: services.wiki.compile(db))


@celery_app.task(name="soulclaw.wiki_lint")
def wiki_lint_task(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    del payload
    return _run_job(job_id, lambda services, db: services.wiki.lint(db))


@celery_app.task(name="soulclaw.wiki_repair")
def wiki_repair_task(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    error_ids = payload.get("error_ids")
    parsed_error_ids = [str(item) for item in error_ids] if isinstance(error_ids, list) else []
    return _run_job(
        job_id,
        lambda services, db: services.wiki.repair(
            db,
            apply_safe=bool(payload.get("apply_safe", True)),
            error_ids=parsed_error_ids,
            llm=services.llm,
        ),
    )


@celery_app.task(name="soulclaw.skill_scan")
def skill_scan_task(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    del payload
    return _run_job(job_id, lambda services, db: services.skills.scan(db))


@celery_app.task(name="soulclaw.embedding_rebuild")
def embedding_rebuild_task(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    source_type = str(payload.get("source_type") or "")
    strict = bool(payload.get("strict", False))
    return _run_job(job_id, lambda services, db: services.vector.rebuild_all(db, source_type=source_type, strict=strict))


@celery_app.task(name="soulclaw.mcp_refresh")
def mcp_refresh_task(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    async def refresh(services):
        server_name = str(payload.get("server_name") or "")
        if server_name:
            return await services.mcp.refresh_server(server_name)
        return await services.mcp.refresh_all()

    return _run_job(job_id, lambda services, db: asyncio.run(refresh(services)))


@celery_app.task(name="soulclaw.heartbeat_check")
def heartbeat_check_task(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    del payload

    def run(services, db):
        result = services.heartbeat.run_check(db)
        return {
            "status": result.status,
            "active_tasks": result.active_tasks,
            "proposals_created": result.proposals_created,
            "proposal_ids": result.proposal_ids,
        }

    return _run_job(job_id, run)


@celery_app.task(name="soulclaw.a2a_sync")
def a2a_sync_task(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    statuses = payload.get("statuses")
    parsed_statuses = [str(item) for item in statuses] if isinstance(statuses, list) else None
    limit = int(payload.get("limit") or 50)
    return _run_job(
        job_id,
        lambda services, db: services.a2a.sync_active_remote_tasks(
            db,
            statuses=parsed_statuses,
            limit=limit,
        ),
    )
