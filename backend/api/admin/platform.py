"""Platform events, runs, approvals, settings, and turn runtime APIs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from backend.api.admin.deps import (
    get_agent_runtime,
    get_current_user,
    get_db,
    get_event_bus,
)
from backend.api.admin.serializers import (
    audit_event_to_dict,
    runtime_event_to_dict,
    tool_run_to_dict,
)
from backend.infra.config import Settings, get_settings
from backend.infra.events import RuntimeEventBus
from backend.infra.models import CronJob, ToolRun, User
from backend.runtime.agent import AgentRuntime

router = APIRouter(tags=["platform"], dependencies=[Depends(get_current_user)])


class TurnRequest(BaseModel):
    message: str
    session_id: str = "local"
    tool_calls: list[dict] | None = None


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
    return {"user": user.username, "turn_id": result.turn_id, "answer": result.answer, "context": result.context}


@router.get("/api/settings")
def settings(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    qdrant = request.app.state.qdrant_index.health()
    redis_client = request.app.state.redis_client
    dream_job = db.scalar(select(CronJob).where(CronJob.name == "system-dream-review"))
    return {
        "app_name": settings.app_name,
        "environment": settings.environment,
        "database_url": "ZLAGENT_DATABASE_URL",
        "database_url_backup_alias": "DATABASE_URL",
        "redis_url": settings.redis_url,
        "redis_available": redis_client is not None,
        "qdrant_enabled": settings.qdrant_enabled,
        "qdrant_url": settings.qdrant_url,
        "qdrant_status": qdrant["status"],
        "qdrant_available": qdrant["available"],
        "qdrant_error": qdrant["error"],
        "wiki_root": str(settings.resolved_wiki_root),
        "skills_root": str(settings.resolved_skills_root),
        "fastembed_cache_dir": str(settings.fastembed_cache_dir),
        "openai_configured": bool(settings.openai_api_key),
        "bootstrap_wiki_on_startup": settings.bootstrap_wiki_on_startup,
        "bootstrap_skills_on_startup": settings.bootstrap_skills_on_startup,
        "mcp_refresh_on_startup": settings.mcp_refresh_on_startup,
        "dream_review_enabled": settings.dream_review_enabled,
        "dream_review_cron": settings.dream_review_cron,
        "dream_review_timezone": settings.dream_review_timezone,
        "dream_review_window_hours": settings.dream_review_window_hours,
        "dream_review_limit": settings.dream_review_limit,
        "dream_review_job_enabled": bool(dream_job.enabled) if dream_job else False,
        "dream_review_next_run_at": dream_job.next_run_at.isoformat() if dream_job and dream_job.next_run_at else None,
    }
