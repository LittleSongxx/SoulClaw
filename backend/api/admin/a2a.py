"""A2A discovery, task, and JSON-RPC endpoints."""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.api.admin.deps import get_a2a_runtime, get_a2a_service, get_current_user, get_db, get_job_service, get_tool_executor
from backend.api.admin.serializers import a2a_connection_to_dict, a2a_task_to_dict, background_job_to_dict
from backend.domain.a2a import A2AService
from backend.domain.jobs import BackgroundJobService, enqueue_background_job
from backend.domain.tools import ToolApprovalRequired, ToolExecutor
from backend.infra.config import Settings, get_settings
from backend.infra.models import User
from backend.runtime.a2a import A2ARuntimeManager

router = APIRouter(tags=["a2a"])


class A2AConnectionRequest(BaseModel):
    name: str
    kind: str = "a2a"
    endpoint: str = ""
    rpc_url: str = ""
    agent_card: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    status: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    skills: list[dict[str, Any]] = Field(default_factory=list)


class A2ADelegateApiRequest(BaseModel):
    capability: str = "deep-research"
    query: str
    context: dict[str, Any] = Field(default_factory=dict)
    files: list[dict[str, Any]] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)
    connection_name: str = ""


class A2AResumeRemoteApprovalRequest(BaseModel):
    decision: str = "approve"
    edited_payload: dict[str, Any] = Field(default_factory=dict)
    response: str = ""


class A2ASyncActiveRequest(BaseModel):
    statuses: list[str] = Field(default_factory=list)
    limit: int = 50
    enqueue: bool = True


@router.get("/.well-known/agent-card.json")
def public_agent_card(runtime: A2ARuntimeManager = Depends(get_a2a_runtime)) -> dict:
    return runtime.agent_card()


@router.post("/api/a2a")
def a2a_jsonrpc(
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
    a2a_version: str | None = Header(default=None, alias="A2A-Version"),
    x_soulclaw_a2a_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
    runtime: A2ARuntimeManager = Depends(get_a2a_runtime),
    settings: Settings = Depends(get_settings),
) -> dict:
    if payload.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32600, "message": "invalid JSON-RPC request"}}
    if a2a_version != "1.0":
        return {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32600, "message": "A2A-Version header must be 1.0"}}
    if not _a2a_public_authorized(settings, authorization=authorization, api_key=x_soulclaw_a2a_key):
        return {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32003, "message": "A2A public API authentication required"}}
    return runtime.handle_jsonrpc(db, payload)


@router.get("/api/a2a/card")
def local_agent_card(runtime: A2ARuntimeManager = Depends(get_a2a_runtime)) -> dict:
    return runtime.agent_card()


@router.get("/api/a2a/connections", dependencies=[Depends(get_current_user)])
def list_a2a_connections(
    enabled: bool | None = None,
    kind: str | None = None,
    db: Session = Depends(get_db),
    service: A2AService = Depends(get_a2a_service),
) -> dict:
    return {
        "items": [
            a2a_connection_to_dict(item)
            for item in service.list_connections(db, enabled=enabled, kind=kind, limit=500)
        ]
    }


@router.post("/api/a2a/connections", dependencies=[Depends(get_current_user)])
def upsert_a2a_connection(
    payload: A2AConnectionRequest,
    db: Session = Depends(get_db),
    service: A2AService = Depends(get_a2a_service),
) -> dict:
    try:
        return a2a_connection_to_dict(service.upsert_connection(db, **payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/a2a/connections/{connection_name}/discover", dependencies=[Depends(get_current_user)])
def discover_a2a_connection(
    connection_name: str,
    db: Session = Depends(get_db),
    runtime: A2ARuntimeManager = Depends(get_a2a_runtime),
) -> dict:
    try:
        return {"agent_card": runtime.discover(db, connection_name)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/a2a/delegate", dependencies=[Depends(get_current_user)])
def delegate_a2a_task(
    payload: A2ADelegateApiRequest,
    db: Session = Depends(get_db),
    executor: ToolExecutor = Depends(get_tool_executor),
    user: User = Depends(get_current_user),
) -> dict:
    try:
        tool_result = executor.execute(db, tool_name="a2a_delegate", arguments=payload.model_dump())
        result = tool_result.get("result") if isinstance(tool_result.get("result"), dict) else tool_result
        result["requested_by"] = user.username
        return result
    except ToolApprovalRequired as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "A2A delegation requires approval",
                "approval_id": exc.approval_id,
                "tool_name": exc.tool_name or "a2a_delegate",
            },
        ) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PermissionError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/a2a/tasks", dependencies=[Depends(get_current_user)])
def list_a2a_tasks(
    status: str | None = None,
    connection_name: str | None = None,
    db: Session = Depends(get_db),
    service: A2AService = Depends(get_a2a_service),
) -> dict:
    return {
        "items": [
            a2a_task_to_dict(item)
            for item in service.list_tasks(db, status=status, connection_name=connection_name, limit=500)
        ]
    }


@router.post("/api/a2a/tasks/sync-active", dependencies=[Depends(get_current_user)])
def sync_active_a2a_tasks(
    payload: A2ASyncActiveRequest,
    db: Session = Depends(get_db),
    runtime: A2ARuntimeManager = Depends(get_a2a_runtime),
    jobs: BackgroundJobService = Depends(get_job_service),
    user: User = Depends(get_current_user),
) -> dict:
    if payload.enqueue:
        job = enqueue_background_job(
            db,
            task_name="a2a_sync",
            payload={"statuses": payload.statuses, "limit": payload.limit},
            triggered_by=user.username,
            service=jobs,
        )
        return {"ok": True, "mode": "job", "job": background_job_to_dict(job)}
    return {"ok": True, "mode": "sync", "result": runtime.sync_active_remote_tasks(db, statuses=payload.statuses, limit=payload.limit)}


@router.get("/api/a2a/tasks/{task_id}", dependencies=[Depends(get_current_user)])
def get_a2a_task(
    task_id: str,
    db: Session = Depends(get_db),
    service: A2AService = Depends(get_a2a_service),
) -> dict:
    task = service.get_task_by_any_id(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"A2A task not found: {task_id}")
    return a2a_task_to_dict(
        task,
        artifacts=service.list_artifacts(db, task.task_id),
        events=service.list_events(db, task.task_id),
    )


@router.post("/api/a2a/tasks/{task_id}/cancel", dependencies=[Depends(get_current_user)])
def cancel_a2a_task(
    task_id: str,
    db: Session = Depends(get_db),
    runtime: A2ARuntimeManager = Depends(get_a2a_runtime),
) -> dict:
    try:
        return runtime.cancel_task(db, task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/a2a/tasks/{task_id}/sync", dependencies=[Depends(get_current_user)])
def sync_a2a_task(
    task_id: str,
    db: Session = Depends(get_db),
    runtime: A2ARuntimeManager = Depends(get_a2a_runtime),
) -> dict:
    try:
        return runtime.sync_remote_task(db, task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/a2a/tasks/{task_id}/resume-remote-approval", dependencies=[Depends(get_current_user)])
def resume_remote_a2a_approval(
    task_id: str,
    payload: A2AResumeRemoteApprovalRequest,
    approval_id: str = "",
    db: Session = Depends(get_db),
    runtime: A2ARuntimeManager = Depends(get_a2a_runtime),
    service: A2AService = Depends(get_a2a_service),
) -> dict:
    task = service.get_task_by_any_id(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"A2A task not found: {task_id}")
    target_approval_id = approval_id or str((task.metadata_json or {}).get("remote_approval_id") or "")
    if not target_approval_id:
        raise HTTPException(status_code=400, detail="remote approval id is required")
    try:
        return runtime.resume_remote_approval(
            db,
            target_approval_id,
            decision=payload.decision,
            edited_payload=payload.edited_payload or None,
            response=payload.response,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/a2a/tasks/{task_id}/events", dependencies=[Depends(get_current_user)])
def stream_a2a_task_events(
    task_id: str,
    after_sequence: int = 0,
    live: bool = True,
    db: Session = Depends(get_db),
    service: A2AService = Depends(get_a2a_service),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    task = service.get_task_by_any_id(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"A2A task not found: {task_id}")

    def generate():
        last_sequence = int(after_sequence or 0)
        idle_started = time.monotonic()
        interval = max(0.25, float(settings.a2a_poll_interval_seconds or 5.0))
        max_idle = max(1, int(settings.a2a_live_event_idle_seconds or 30))
        while True:
            emitted = False
            for item in service.list_events(db, task.task_id, after_sequence=last_sequence):
                emitted = True
                idle_started = time.monotonic()
                last_sequence = max(last_sequence, int(item.sequence or 0))
                payload = {"type": item.event_type, "sequence": item.sequence, "data": item.payload or {}}
                yield f"id: {item.sequence}\nevent: {item.event_type}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
            if not live:
                break
            if not emitted:
                if time.monotonic() - idle_started >= max_idle:
                    yield f": idle {last_sequence}\n\n"
                    break
                yield f": heartbeat {last_sequence}\n\n"
                time.sleep(interval)

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/api/a2a/callbacks/soulsearcher")
def soulsearcher_a2a_callback(
    payload: dict[str, Any],
    x_soulsearcher_callback_token: str | None = Header(default=None, alias="X-SoulSearcher-Callback-Token"),
    db: Session = Depends(get_db),
    runtime: A2ARuntimeManager = Depends(get_a2a_runtime),
    settings: Settings = Depends(get_settings),
) -> dict:
    expected = str(settings.a2a_callback_secret or "")
    if expected and x_soulsearcher_callback_token != expected:
        raise HTTPException(status_code=403, detail="invalid SoulSearcher callback token")
    try:
        return runtime.handle_callback(db, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _a2a_public_authorized(settings: Settings, *, authorization: str | None, api_key: str | None) -> bool:
    if not settings.a2a_require_public_auth:
        return True
    expected = settings.a2a_public_api_key
    if not expected:
        return False
    token = api_key or ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    return token == expected
