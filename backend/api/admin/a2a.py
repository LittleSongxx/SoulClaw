"""A2A discovery, task, and JSON-RPC endpoints."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.api.admin.deps import get_a2a_runtime, get_a2a_service, get_current_user, get_db
from backend.api.admin.serializers import a2a_connection_to_dict, a2a_task_to_dict
from backend.domain.a2a import A2AService
from backend.infra.models import User
from backend.runtime.a2a import A2ADelegateRequest, A2ARuntimeManager

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


@router.get("/.well-known/agent-card.json")
def public_agent_card(runtime: A2ARuntimeManager = Depends(get_a2a_runtime)) -> dict:
    return runtime.agent_card()


@router.get("/.well-known/agent-card")
def public_agent_card_legacy(runtime: A2ARuntimeManager = Depends(get_a2a_runtime)) -> dict:
    return runtime.agent_card()


@router.post("/api/a2a")
def a2a_jsonrpc(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    runtime: A2ARuntimeManager = Depends(get_a2a_runtime),
) -> dict:
    if payload.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32600, "message": "invalid JSON-RPC request"}}
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
    runtime: A2ARuntimeManager = Depends(get_a2a_runtime),
    user: User = Depends(get_current_user),
) -> dict:
    try:
        request = A2ADelegateRequest(**payload.model_dump())
        if runtime.requires_approval(request.capability, request.options):
            raise HTTPException(status_code=409, detail="A2A delegation requires approval through the tool surface")
        result = runtime.delegate(db, request)
        result["requested_by"] = user.username
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
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
        artifacts=service.list_artifacts(db, task_id),
        events=service.list_events(db, task_id),
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


@router.get("/api/a2a/tasks/{task_id}/events", dependencies=[Depends(get_current_user)])
def stream_a2a_task_events(
    task_id: str,
    after_sequence: int = 0,
    db: Session = Depends(get_db),
    service: A2AService = Depends(get_a2a_service),
) -> StreamingResponse:
    task = service.get_task_by_any_id(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"A2A task not found: {task_id}")
    events = service.list_events(db, task_id, after_sequence=after_sequence)

    def generate():
        for item in events:
            payload = {"type": item.event_type, "sequence": item.sequence, "data": item.payload or {}}
            yield f"id: {item.sequence}\nevent: {item.event_type}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
