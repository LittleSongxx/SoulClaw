"""Platform events, runs, approvals, settings, and turn runtime APIs."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from backend.api.admin.deps import (
    get_agent_runtime,
    get_conversation_service,
    get_current_user,
    get_db,
    get_event_bus,
    get_job_service,
    get_workspace_service,
)
from backend.api.admin.serializers import (
    audit_event_to_dict,
    background_job_to_dict,
    runtime_event_to_dict,
    session_message_to_dict,
    session_summary_to_dict,
    tool_run_to_dict,
)
from backend.domain.conversation import ConversationService
from backend.domain.jobs import BackgroundJobService
from backend.domain.workspace import WorkspaceService
from backend.infra.config import Settings, get_settings
from backend.infra.db import database_backend, migration_status
from backend.infra.events import RuntimeEventBus
from backend.infra.health import readiness_summary
from backend.infra.models import (
    AuditEvent,
    BackgroundJob,
    CronJob,
    IdempotencyRecord,
    OutboxMessage,
    RuntimeEvent,
    ToolRun,
    User,
)
from backend.infra.observability import reliability_alerts
from backend.runtime.agent import AgentRuntime

router = APIRouter(tags=["platform"], dependencies=[Depends(get_current_user)])


class TurnRequest(BaseModel):
    message: str
    session_id: str = "local"
    tool_calls: list[dict] | None = None


class WorkspaceFileUpdateRequest(BaseModel):
    content: str


@router.get("/api/events")
def runtime_events(
    event_type: str | None = None,
    limit: int = 100,
    events: RuntimeEventBus = Depends(get_event_bus),
) -> dict:
    return {"items": [runtime_event_to_dict(item) for item in events.list_runtime_events(limit=limit, event_type=event_type)]}


@router.get("/api/audit")
def audit_events(limit: int = 100, events: RuntimeEventBus = Depends(get_event_bus)) -> dict:
    return {"items": [audit_event_to_dict(item) for item in events.list_audit_events(limit=limit)]}


@router.get("/api/runs")
def runs(limit: int = 100, db: Session = Depends(get_db)) -> dict:
    limit = max(1, min(limit, 500))
    stmt = select(ToolRun).order_by(desc(ToolRun.started_at)).limit(limit)
    return {"items": [tool_run_to_dict(item) for item in db.scalars(stmt).all()]}


@router.post("/api/runs/turn")
def run_turn(
    payload: TurnRequest,
    db: Session = Depends(get_db),
    runtime: AgentRuntime = Depends(get_agent_runtime),
    user: User = Depends(get_current_user),
) -> dict:
    result = runtime.run_turn(db, payload.message, session_id=payload.session_id, tool_calls=payload.tool_calls)
    return {
        "user": user.username,
        "turn_id": result.turn_id,
        "answer": result.answer,
        "context": result.context,
        "status": result.status,
        "approval_id": result.approval_id,
        "pending_tool_call": result.pending_tool_call or {},
        "resume_available": result.resume_available,
    }


@router.get("/api/sessions")
def sessions(
    limit: int = 100,
    db: Session = Depends(get_db),
    conversation: ConversationService = Depends(get_conversation_service),
) -> dict:
    items = conversation.list_sessions(db, limit=limit)
    return {
        "items": [
            {
                **item,
                "last_message_at": item["last_message_at"].isoformat() if item.get("last_message_at") else None,
            }
            for item in items
        ]
    }


@router.get("/api/sessions/{session_id}/messages")
def session_messages(
    session_id: str,
    limit: int = 100,
    db: Session = Depends(get_db),
    conversation: ConversationService = Depends(get_conversation_service),
) -> dict:
    return {"items": [session_message_to_dict(item) for item in conversation.list_messages(db, session_id, limit=limit)]}


@router.get("/api/sessions/{session_id}/summary")
def session_summary(
    session_id: str,
    db: Session = Depends(get_db),
    conversation: ConversationService = Depends(get_conversation_service),
) -> dict:
    return {"summary": session_summary_to_dict(conversation.get_summary(db, session_id))}


@router.get("/api/jobs")
def jobs(
    status: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    service: BackgroundJobService = Depends(get_job_service),
) -> dict:
    return {"items": [background_job_to_dict(item) for item in service.list(db, status=status, limit=limit)]}


@router.get("/api/jobs/{job_id}")
def job(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    service: BackgroundJobService = Depends(get_job_service),
) -> dict:
    item = service.get(db, job_id)
    if item is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="job not found")
    return background_job_to_dict(item)


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    service: BackgroundJobService = Depends(get_job_service),
) -> dict:
    from fastapi import HTTPException

    try:
        return {"ok": True, "job": background_job_to_dict(service.cancel(db, job_id))}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/reliability")
def reliability_status(request: Request, db: Session = Depends(get_db)) -> dict:
    resilience = getattr(request.app.state, "resilience", None)
    rate_limiter = getattr(request.app.state, "rate_limiter", None)
    observability = getattr(request.app.state, "observability", None)
    resilience_state = resilience.state() if resilience is not None and hasattr(resilience, "state") else {}
    rate_limit_state = rate_limiter.state() if rate_limiter is not None and hasattr(rate_limiter, "state") else {}
    counts = {
        "jobs": _status_counts(db, BackgroundJob.status),
        "outbox": _status_counts(db, OutboxMessage.status),
        "idempotency": _status_counts(db, IdempotencyRecord.status),
        "cron": {
            "status": _status_counts(db, CronJob.last_status),
            "backoff": int(db.scalar(select(func.count()).select_from(CronJob).where(CronJob.backoff_until.is_not(None))) or 0),
        },
    }
    if observability is not None and hasattr(observability, "collect_reliability"):
        observability.collect_reliability(db, resilience_state=resilience_state, rate_limit_state=rate_limit_state)
    return {
        **counts,
        "resilience": resilience_state,
        "rate_limit": rate_limit_state,
        "observability": {
            "metrics_enabled": bool(getattr(request.app.state.settings, "metrics_enabled", True)) if hasattr(request.app.state, "settings") else True,
            "tracing": getattr(request.app.state, "otel", {}),
        },
    }


@router.get("/api/reliability/alerts")
def reliability_alerts_api(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    resilience = getattr(request.app.state, "resilience", None)
    items = reliability_alerts(
        db,
        settings=settings,
        redis_client=getattr(request.app.state, "redis_client", None),
        resilience_state=resilience.state() if resilience is not None and hasattr(resilience, "state") else {},
    )
    return {"ok": not any(item.get("severity") == "critical" for item in items), "items": items}


@router.get("/api/reliability/limits")
def reliability_limits(request: Request) -> dict:
    rate_limiter = getattr(request.app.state, "rate_limiter", None)
    return rate_limiter.state() if rate_limiter is not None and hasattr(rate_limiter, "state") else {"enabled": False}


@router.get("/api/traces/{trace_id}")
def trace_detail(trace_id: str, db: Session = Depends(get_db)) -> dict:
    trace_id = trace_id.strip()[:128]
    events_stmt = select(RuntimeEvent).where(RuntimeEvent.trace_id == trace_id).order_by(desc(RuntimeEvent.created_at)).limit(200)
    audit_stmt = select(AuditEvent).where(AuditEvent.trace_id == trace_id).order_by(desc(AuditEvent.created_at)).limit(100)
    jobs_stmt = select(BackgroundJob).where(BackgroundJob.trace_id == trace_id).order_by(desc(BackgroundJob.created_at)).limit(100)
    outbox_stmt = select(OutboxMessage).where(OutboxMessage.trace_id == trace_id).order_by(desc(OutboxMessage.created_at)).limit(100)
    return {
        "trace_id": trace_id,
        "events": [runtime_event_to_dict(item) for item in db.scalars(events_stmt).all()],
        "audit": [audit_event_to_dict(item) for item in db.scalars(audit_stmt).all()],
        "jobs": [background_job_to_dict(item) for item in db.scalars(jobs_stmt).all()],
        "outbox": [_outbox_to_dict(item) for item in db.scalars(outbox_stmt).all()],
    }


@router.get("/api/settings")
def settings(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    redis_client = getattr(request.app.state, "redis_client", None)
    dream_job = db.scalar(select(CronJob).where(CronJob.name == "system-dream-review"))
    migrations = migration_status(settings)
    readiness = readiness_summary(settings, redis_client)
    return {
        "app_name": settings.app_name,
        "environment": settings.environment,
        "database_url": "SOULCLAW_DATABASE_URL",
        "database_url_backup_alias": "DATABASE_URL",
        "database_backend": database_backend(settings.database_url),
        "migration_revision": migrations.get("current_revision"),
        "migration_head_revision": migrations.get("head_revision"),
        "migration_current": migrations.get("is_current"),
        "readiness": {
            "ok": readiness["ok"],
            "checks": {
                name: {
                    "ok": check.get("ok", False),
                    "configured": check.get("configured"),
                    "required": check.get("required"),
                }
                for name, check in readiness["checks"].items()
            },
        },
        "redis_url": "SOULCLAW_REDIS_URL",
        "redis_available": redis_client is not None,
        "redis_required": settings.redis_required,
        "celery_broker_url": "SOULCLAW_CELERY_BROKER_URL or SOULCLAW_REDIS_URL",
        "celery_result_backend": "SOULCLAW_CELERY_RESULT_BACKEND or SOULCLAW_REDIS_URL",
        "api_scheduler_enabled": settings.api_scheduler_enabled,
        "wiki_root": str(settings.resolved_wiki_root),
        "skills_root": str(settings.resolved_skills_root),
        "workspace_dir": str(settings.workspace_dir),
        "workspace_seed_dir": str(settings.workspace_seed_dir),
        "openai_configured": bool(settings.openai_api_key),
        "llm_provider": settings.llm_provider,
        "llm_context_window_tokens": settings.llm_context_window_tokens,
        "cors_origins": settings.cors_origins,
        "require_production_secrets": settings.require_production_secrets,
        "login_rate_limit_enabled": settings.login_rate_limit_enabled,
        "metrics_enabled": settings.metrics_enabled,
        "metrics_path": settings.metrics_path,
        "tracing_enabled": settings.tracing_enabled,
        "otel_exporter_otlp_configured": bool(settings.otel_exporter_otlp_endpoint),
        "rate_limit_enabled": settings.rate_limit_enabled,
        "rate_limit_memory_fallback": settings.rate_limit_memory_fallback,
        "bootstrap_wiki_on_startup": settings.bootstrap_wiki_on_startup,
        "bootstrap_skills_on_startup": settings.bootstrap_skills_on_startup,
        "mcp_refresh_on_startup": settings.mcp_refresh_on_startup,
        "mcp_seed_on_startup": settings.mcp_seed_on_startup,
        "mcp_config_file": str(settings.mcp_config_file),
        "tool_schema_direct_limit": settings.tool_schema_direct_limit,
        "dream_review_enabled": settings.dream_review_enabled,
        "dream_review_cron": settings.dream_review_cron,
        "dream_review_timezone": settings.dream_review_timezone,
        "dream_review_window_hours": settings.dream_review_window_hours,
        "dream_review_limit": settings.dream_review_limit,
        "dream_review_job_enabled": bool(dream_job.enabled) if dream_job else False,
        "dream_review_next_run_at": dream_job.next_run_at.isoformat() if dream_job and dream_job.next_run_at else None,
        "heartbeat_enabled": settings.heartbeat_enabled,
        "heartbeat_cron": settings.heartbeat_cron,
        "heartbeat_timezone": settings.heartbeat_timezone,
        "gateway_heartbeat_timeout_seconds": settings.gateway_heartbeat_timeout_seconds,
        "gateway_webhook_max_skew_seconds": settings.gateway_webhook_max_skew_seconds,
        "gateway_webhook_nonce_cache_size": settings.gateway_webhook_nonce_cache_size,
        "a2a_bootstrap_soulsearcher_enabled": settings.a2a_bootstrap_soulsearcher_enabled,
        "a2a_require_public_auth": settings.a2a_require_public_auth,
        "a2a_public_api_key_configured": bool(settings.a2a_public_api_key),
    }


@router.get("/api/workspace/files")
def workspace_files(service: WorkspaceService = Depends(get_workspace_service)) -> dict:
    return {
        "items": [
            {
                "kind": item.kind,
                "path": item.path,
                "content": item.content,
                "updated_at": item.updated_at,
            }
            for item in service.read_all().values()
        ],
        "history": service.read_history(limit=20),
    }


@router.get("/api/workspace/files/{kind}")
def workspace_file(kind: str, service: WorkspaceService = Depends(get_workspace_service)) -> dict:
    try:
        item = service.read(kind)
    except KeyError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"kind": item.kind, "path": item.path, "content": item.content, "updated_at": item.updated_at}


@router.put("/api/workspace/files/{kind}")
def update_workspace_file(
    kind: str,
    payload: WorkspaceFileUpdateRequest,
    service: WorkspaceService = Depends(get_workspace_service),
    user: User = Depends(get_current_user),
) -> dict:
    try:
        item = service.write(kind, payload.content, actor=user.username)
    except KeyError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"kind": item.kind, "path": item.path, "content": item.content, "updated_at": item.updated_at}


def _status_counts(db: Session, column) -> dict[str, int]:
    rows = db.execute(select(column, func.count()).group_by(column)).all()
    return {str(status or "unknown"): int(count or 0) for status, count in rows}


def _outbox_to_dict(item: OutboxMessage) -> dict:
    from backend.api.admin.serializers import dt

    return {
        "id": str(item.id),
        "trace_id": getattr(item, "trace_id", "") or "",
        "request_id": getattr(item, "request_id", "") or "",
        "topic": item.topic,
        "aggregate_type": item.aggregate_type,
        "aggregate_id": item.aggregate_id,
        "idempotency_key": item.idempotency_key,
        "status": item.status,
        "attempt_count": item.attempt_count,
        "max_attempts": item.max_attempts,
        "next_attempt_at": dt(item.next_attempt_at),
        "last_error": item.last_error,
        "payload": item.payload or {},
        "created_at": dt(item.created_at),
        "dispatched_at": dt(item.dispatched_at),
    }
