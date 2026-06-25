"""Cron, MCP, gateway, and approval management API."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.api.admin.deps import (
    get_current_user,
    get_db,
    get_gateway_runtime,
    get_mcp_runtime,
    get_platform_service,
    get_tool_registry,
)
from backend.api.admin.serializers import (
    approval_to_dict,
    cron_job_to_dict,
    gateway_to_dict,
    mcp_server_to_dict,
)
from backend.domain.platform import PlatformService
from backend.domain.tools import ToolRegistry
from backend.runtime.gateway import (
    GatewayRuntimeManager,
    InboundGatewayMessage,
    OutboundGatewayMessage,
)
from backend.runtime.mcp import MCPRuntimeManager

router = APIRouter(tags=["control"], dependencies=[Depends(get_current_user)])


class ApprovalCreateRequest(BaseModel):
    subject_type: str
    subject_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class ApprovalResolveRequest(BaseModel):
    status: str


class CronJobRequest(BaseModel):
    name: str
    cron_expr: str
    timezone: str = "Asia/Hong_Kong"
    instruction: str = ""
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class MCPServerRequest(BaseModel):
    name: str
    transport: str = "stdio"
    command: str = ""
    url: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class GatewayRequest(BaseModel):
    name: str
    kind: str
    endpoint: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = False
    status: str | None = None


class GatewayInboundRequest(BaseModel):
    gateway_name: str
    external_user_id: str
    text: str
    channel_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewaySendRequest(BaseModel):
    gateway_name: str
    target_id: str
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.get("/api/approvals")
def approvals(
    status: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    service: PlatformService = Depends(get_platform_service),
) -> dict:
    return {"items": [approval_to_dict(item) for item in service.list_approvals(db, status=status, limit=limit)]}


@router.post("/api/approvals")
def create_approval(
    payload: ApprovalCreateRequest,
    db: Session = Depends(get_db),
    service: PlatformService = Depends(get_platform_service),
) -> dict:
    return approval_to_dict(service.create_approval(db, **payload.model_dump()))


@router.post("/api/approvals/{approval_id}/resolve")
def resolve_approval(
    approval_id: uuid.UUID,
    payload: ApprovalResolveRequest,
    db: Session = Depends(get_db),
    service: PlatformService = Depends(get_platform_service),
) -> dict:
    try:
        return {"ok": True, "approval": approval_to_dict(service.resolve_approval(db, approval_id, status=payload.status))}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/cron")
def list_cron(
    enabled: bool | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    service: PlatformService = Depends(get_platform_service),
) -> dict:
    return {"items": [cron_job_to_dict(item) for item in service.list_cron_jobs(db, enabled=enabled, limit=limit)]}


@router.post("/api/cron")
def upsert_cron(
    payload: CronJobRequest,
    db: Session = Depends(get_db),
    service: PlatformService = Depends(get_platform_service),
) -> dict:
    try:
        return cron_job_to_dict(service.upsert_cron_job(db, **payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/mcp")
def list_mcp(
    enabled: bool | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    service: PlatformService = Depends(get_platform_service),
) -> dict:
    return {"items": [mcp_server_to_dict(item) for item in service.list_mcp_servers(db, enabled=enabled, limit=limit)]}


@router.post("/api/mcp")
def upsert_mcp(
    payload: MCPServerRequest,
    db: Session = Depends(get_db),
    service: PlatformService = Depends(get_platform_service),
) -> dict:
    try:
        return mcp_server_to_dict(service.upsert_mcp_server(db, **payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/mcp/refresh")
async def refresh_mcp(
    runtime: MCPRuntimeManager = Depends(get_mcp_runtime),
    registry: ToolRegistry = Depends(get_tool_registry),
) -> dict:
    result = await runtime.refresh_all()
    result["registry"] = runtime.install_into_registry(registry)
    return result


@router.post("/api/mcp/{server_name}/refresh")
async def refresh_mcp_server(
    server_name: str,
    runtime: MCPRuntimeManager = Depends(get_mcp_runtime),
    registry: ToolRegistry = Depends(get_tool_registry),
) -> dict:
    try:
        result = await runtime.refresh_server(server_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    result["registry"] = runtime.install_into_registry(registry)
    return result


@router.get("/api/gateways")
def list_gateways(
    kind: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    service: PlatformService = Depends(get_platform_service),
) -> dict:
    return {"items": [gateway_to_dict(item) for item in service.list_gateways(db, kind=kind, limit=limit)]}


@router.post("/api/gateways")
def upsert_gateway(
    payload: GatewayRequest,
    db: Session = Depends(get_db),
    service: PlatformService = Depends(get_platform_service),
) -> dict:
    try:
        return gateway_to_dict(service.upsert_gateway(db, **payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/gateways/inbound")
def gateway_inbound(
    payload: GatewayInboundRequest,
    runtime: GatewayRuntimeManager = Depends(get_gateway_runtime),
) -> dict:
    try:
        return runtime.handle_inbound(InboundGatewayMessage(**payload.model_dump()))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/gateways/send")
def gateway_send(
    payload: GatewaySendRequest,
    runtime: GatewayRuntimeManager = Depends(get_gateway_runtime),
) -> dict:
    try:
        return runtime.send(OutboundGatewayMessage(**payload.model_dump()))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, ValueError, NotImplementedError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
