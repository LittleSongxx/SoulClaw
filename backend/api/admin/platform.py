"""Platform events, runs, approvals, settings, and turn runtime APIs."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import desc, select
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
from backend.infra.models import CronJob, ToolRun, User
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
        "a2a_bootstrap_weaver_enabled": settings.a2a_bootstrap_weaver_enabled,
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
