"""Cron, MCP, gateway, and approval management API."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from backend.api.admin.deps import (
    get_agent_runtime,
    get_a2a_runtime,
    get_core_context_service,
    get_current_user,
    get_db,
    get_evolution_proposal_service,
    get_evolution_service,
    get_gateway_runtime,
    get_heartbeat_runtime,
    get_job_service,
    get_mcp_runtime,
    get_platform_service,
    get_tool_executor,
    get_tool_registry,
)
from backend.api.admin.serializers import (
    approval_to_dict,
    cron_job_to_dict,
    gateway_to_dict,
    mcp_server_to_dict,
    proposal_to_dict,
)
from backend.domain.evolution import EvolutionService
from backend.domain.evolution_proposals import EvolutionProposalService
from backend.domain.core_context import CoreContextService
from backend.domain.jobs import BackgroundJobService, enqueue_background_job
from backend.domain.platform import PlatformService
from backend.domain.tools import ToolExecutor, ToolRegistry
from backend.infra.config import Settings, get_settings
from backend.infra.models import Approval, BackgroundJob, GatewayConnection, User
from backend.runtime.agent import AgentRuntime
from backend.runtime.a2a import A2ARuntimeManager
from backend.runtime.gateway import (
    GatewayRuntimeManager,
    InboundGatewayMessage,
    OutboundGatewayMessage,
)
from backend.runtime.heartbeat import HeartbeatRuntime
from backend.runtime.mcp import MCPRuntimeManager

router = APIRouter(tags=["control"], dependencies=[Depends(get_current_user)])
public_router = APIRouter(tags=["gateway-webhook"])


class ApprovalCreateRequest(BaseModel):
    subject_type: str
    subject_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    turn_checkpoint: dict[str, Any] = Field(default_factory=dict)
    original_tool_call: dict[str, Any] = Field(default_factory=dict)
    allowed_decisions: list[str] = Field(default_factory=lambda: ["approve", "edit", "reject", "respond"])
    resume_state: dict[str, Any] = Field(default_factory=dict)


class ApprovalResolveRequest(BaseModel):
    status: str
    edited_arguments: dict[str, Any] = Field(default_factory=dict)
    response: str = ""


class ApprovalRejectRequest(BaseModel):
    reason: str = ""


class ApprovalResumeTurnRequest(BaseModel):
    decision: str = "approve"
    edited_arguments: dict[str, Any] = Field(default_factory=dict)
    response: str = ""


class EvolutionApplyRequest(BaseModel):
    actor: str | None = None


class EvolutionProposalCreateRequest(BaseModel):
    target_type: str
    action: str
    risk_level: str = "medium"
    payload: dict[str, Any]
    evidence: dict[str, Any] = Field(default_factory=dict)


class EvolutionProposalRejectRequest(BaseModel):
    actor: str | None = None
    reason: str = ""


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
    config_source: str = "manual"
    permission_policy: dict[str, Any] = Field(default_factory=dict)
    session_mode: str = "transient"
    health_status: str = "unknown"
    last_imported_checksum: str = ""


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


class GatewayWebhookRequest(BaseModel):
    gateway_name: str
    external_user_id: str
    text: str
    channel_id: str = ""
    timestamp: str
    nonce: str
    signature: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewaySendRequest(BaseModel):
    gateway_name: str
    target_id: str
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewayHeartbeatRequest(BaseModel):
    instance_id: str = ""
    version: str = ""
    capabilities: list[str] = Field(default_factory=list)


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
        approval = service.resolve_approval(db, approval_id, status=payload.status)
        if payload.edited_arguments:
            approval.edited_arguments = payload.edited_arguments
        if payload.response:
            approval.resume_state = {**(approval.resume_state or {}), "response": payload.response}
        return {"ok": True, "approval": approval_to_dict(approval)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/approvals/{approval_id}/approve-and-run")
def approve_and_run(
    approval_id: uuid.UUID,
    db: Session = Depends(get_db),
    service: PlatformService = Depends(get_platform_service),
    executor: ToolExecutor = Depends(get_tool_executor),
    runtime: AgentRuntime = Depends(get_agent_runtime),
    a2a_runtime: A2ARuntimeManager = Depends(get_a2a_runtime),
) -> dict:
    approval = db.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail=f"approval not found: {approval_id}")
    payload = approval.payload or {}
    if approval.subject_type == "a2a_remote_hitl":
        try:
            result = a2a_runtime.resume_remote_approval(db, approval_id, decision="approve")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "approval": approval_to_dict(approval), "a2a": result}
    if approval.subject_type != "tool_run":
        raise HTTPException(status_code=400, detail="approval is not for a tool run")
    if approval.status not in {"pending", "approved"}:
        raise HTTPException(status_code=400, detail="approval is not runnable")
    checkpoint_mode = (approval.turn_checkpoint or {}).get("mode")
    resume_mode = (approval.resume_state or {}).get("mode")
    if checkpoint_mode == "agent_turn" or resume_mode == "resume_turn":
        try:
            result = runtime.resume_turn(db, approval_id, decision="approve")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "approval": approval_to_dict(approval),
            "turn": {
                "turn_id": result.turn_id,
                "answer": result.answer,
                "context": result.context,
                "status": result.status,
                "approval_id": result.approval_id,
                "pending_tool_call": result.pending_tool_call or {},
                "resume_available": result.resume_available,
            },
        }
    tool_name = str(payload.get("tool_name") or "")
    if not tool_name:
        raise HTTPException(status_code=400, detail="approval payload is missing tool_name")
    if approval.status == "pending":
        try:
            approval = service.resolve_approval(db, approval_id, status="approved")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    arguments = approval.edited_arguments if approval.edited_arguments else payload.get("arguments")
    try:
        result = executor.execute(
            db,
            tool_name=tool_name,
            arguments=arguments if isinstance(arguments, dict) else {},
            turn_id=str(payload.get("turn_id") or ""),
            approved=True,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PermissionError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "approval": approval_to_dict(approval), "tool_result": result}


@router.post("/api/approvals/{approval_id}/resume-turn")
def resume_turn(
    approval_id: uuid.UUID,
    payload: ApprovalResumeTurnRequest,
    db: Session = Depends(get_db),
    runtime: AgentRuntime = Depends(get_agent_runtime),
    a2a_runtime: A2ARuntimeManager = Depends(get_a2a_runtime),
) -> dict:
    approval = db.get(Approval, approval_id)
    if approval is not None and approval.subject_type == "a2a_remote_hitl":
        try:
            result = a2a_runtime.resume_remote_approval(
                db,
                approval_id,
                decision=payload.decision,
                edited_payload=payload.edited_arguments or None,
                response=payload.response,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "approval": approval_to_dict(db.get(Approval, approval_id)), "a2a": result}
    try:
        result = runtime.resume_turn(
            db,
            approval_id,
            decision=payload.decision,
            edited_arguments=payload.edited_arguments,
            response=payload.response,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PermissionError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    approval = db.get(Approval, approval_id)
    return {
        "ok": True,
        "approval": approval_to_dict(approval) if approval is not None else None,
        "turn": {
            "turn_id": result.turn_id,
            "answer": result.answer,
            "context": result.context,
            "status": result.status,
            "approval_id": result.approval_id,
            "pending_tool_call": result.pending_tool_call or {},
            "resume_available": result.resume_available,
        },
    }


@router.post("/api/approvals/{approval_id}/reject")
def reject_approval(
    approval_id: uuid.UUID,
    payload: ApprovalRejectRequest,
    db: Session = Depends(get_db),
    service: PlatformService = Depends(get_platform_service),
    a2a_runtime: A2ARuntimeManager = Depends(get_a2a_runtime),
) -> dict:
    approval = db.get(Approval, approval_id)
    if approval is not None and approval.subject_type == "a2a_remote_hitl":
        try:
            result = a2a_runtime.resume_remote_approval(db, approval_id, decision="reject", response=payload.reason)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        approval = db.get(Approval, approval_id)
        return {"ok": True, "approval": approval_to_dict(approval) if approval is not None else None, "a2a": result}
    try:
        approval = service.resolve_approval(db, approval_id, status="rejected")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    approval.payload = {**(approval.payload or {}), "reject_reason": payload.reason}
    return {"ok": True, "approval": approval_to_dict(approval)}


@router.get("/api/evolution/proposals")
def list_evolution_proposals(
    status: str | None = None,
    target_type: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    service: EvolutionProposalService = Depends(get_evolution_proposal_service),
) -> dict:
    try:
        items = service.list(db, status=status, target_type=target_type, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"items": [proposal_to_dict(item) for item in items]}


@router.post("/api/evolution/proposals")
def create_evolution_proposal(
    payload: EvolutionProposalCreateRequest,
    db: Session = Depends(get_db),
    service: EvolutionProposalService = Depends(get_evolution_proposal_service),
    user: User = Depends(get_current_user),
) -> dict:
    try:
        proposal = service.create(
            db,
            target_type=payload.target_type,
            action=payload.action,
            payload=payload.payload,
            evidence={**payload.evidence, "actor": user.username},
            risk_level=payload.risk_level,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return proposal_to_dict(proposal)


@router.post("/api/evolution/proposals/{proposal_id}/apply")
def apply_evolution_proposal(
    proposal_id: uuid.UUID,
    payload: EvolutionApplyRequest,
    db: Session = Depends(get_db),
    service: EvolutionService = Depends(get_evolution_service),
    user: User = Depends(get_current_user),
) -> dict:
    try:
        proposal = service.apply(db, proposal_id, actor=payload.actor or user.username)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return proposal_to_dict(proposal)


@router.post("/api/evolution/proposals/{proposal_id}/reject")
def reject_evolution_proposal(
    proposal_id: uuid.UUID,
    payload: EvolutionProposalRejectRequest,
    db: Session = Depends(get_db),
    service: EvolutionProposalService = Depends(get_evolution_proposal_service),
    user: User = Depends(get_current_user),
) -> dict:
    try:
        proposal = service.reject(db, proposal_id, actor=payload.actor or user.username, reason=payload.reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return proposal_to_dict(proposal)


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


@router.get("/api/gateways/status")
def gateway_status(
    db: Session = Depends(get_db),
    service: PlatformService = Depends(get_platform_service),
    settings: Settings = Depends(get_settings),
) -> dict:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    items = []
    for gateway in service.list_gateways(db, limit=500):
        item = gateway_to_dict(gateway)
        last = gateway.last_heartbeat_at or gateway.last_inbound_at or gateway.last_outbound_at
        stale = True
        if last is not None:
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            stale = (now - last).total_seconds() > settings.gateway_heartbeat_timeout_seconds
        item["computed_status"] = "offline" if not gateway.enabled else ("stale" if stale else "online")
        items.append(item)
    return {"items": items}


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


@router.post("/api/gateways/{gateway_name}/heartbeat")
def gateway_heartbeat(
    gateway_name: str,
    payload: GatewayHeartbeatRequest,
    db: Session = Depends(get_db),
) -> dict:
    from datetime import UTC, datetime

    gateway = db.scalar(select(GatewayConnection).where(GatewayConnection.name == gateway_name))
    if gateway is None:
        raise HTTPException(status_code=404, detail=f"gateway not found: {gateway_name}")
    gateway.last_heartbeat_at = datetime.now(UTC)
    gateway.instance_id = payload.instance_id
    gateway.version = payload.version
    gateway.capabilities = payload.capabilities
    gateway.status = "online" if gateway.enabled else "disabled"
    return {"ok": True, "gateway": gateway_to_dict(gateway)}


@router.post("/api/gateways/inbound")
def gateway_inbound(
    payload: GatewayInboundRequest,
    x_soulclaw_signature: str | None = Header(default=None),
    runtime: GatewayRuntimeManager = Depends(get_gateway_runtime),
) -> dict:
    try:
        data = payload.model_dump()
        if x_soulclaw_signature:
            data["metadata"] = {**data.get("metadata", {}), "signature": x_soulclaw_signature}
        return runtime.handle_inbound(InboundGatewayMessage(**data))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@public_router.post("/api/gateways/webhook")
def gateway_webhook(
    payload: GatewayWebhookRequest,
    runtime: GatewayRuntimeManager = Depends(get_gateway_runtime),
) -> dict:
    try:
        metadata = {
            **payload.metadata,
            "timestamp": payload.timestamp,
            "nonce": payload.nonce,
            "signature": payload.signature,
            "public_webhook": True,
        }
        return runtime.handle_inbound(
            InboundGatewayMessage(
                gateway_name=payload.gateway_name,
                external_user_id=payload.external_user_id,
                text=payload.text,
                channel_id=payload.channel_id,
                metadata=metadata,
            )
        )
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


@router.post("/api/heartbeat/run")
def run_heartbeat(
    enqueue: bool = True,
    db: Session = Depends(get_db),
    runtime: HeartbeatRuntime = Depends(get_heartbeat_runtime),
    jobs: BackgroundJobService = Depends(get_job_service),
    user: User = Depends(get_current_user),
) -> dict:
    if enqueue:
        job = enqueue_background_job(
            db,
            task_name="heartbeat_check",
            payload={},
            triggered_by=user.username,
            service=jobs,
        )
        from backend.api.admin.serializers import background_job_to_dict

        return {"requested_by": user.username, "job": background_job_to_dict(job)}
    result = runtime.run_check(db)
    return {"requested_by": user.username, **result.__dict__}


@router.get("/api/heartbeat/status")
def heartbeat_status(
    db: Session = Depends(get_db),
    core_context: CoreContextService = Depends(get_core_context_service),
) -> dict:
    latest_job = db.scalar(
        select(BackgroundJob)
        .where(BackgroundJob.task_name == "heartbeat_check")
        .order_by(desc(BackgroundJob.created_at))
        .limit(1)
    )
    from backend.api.admin.serializers import background_job_to_dict

    return {
        "active_tasks": core_context.heartbeat_tasks(db),
        "history": [],
        "latest_job": background_job_to_dict(latest_job) if latest_job else None,
    }
