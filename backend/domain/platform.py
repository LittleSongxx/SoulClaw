"""Platform control-plane services for approvals, Cron, MCP, and gateways."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from croniter import croniter
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from backend.domain.cron_schedule import compute_next_run
from backend.infra.config import Settings, get_settings
from backend.infra.events import RuntimeEventBus
from backend.infra.models import Approval, CronJob, GatewayConnection, MCPServer


class PlatformService:
    def __init__(self, events: RuntimeEventBus | None = None) -> None:
        self.events = events

    def create_approval(
        self,
        db: Session,
        *,
        subject_type: str,
        subject_id: str = "",
        payload: dict[str, Any] | None = None,
        turn_checkpoint: dict[str, Any] | None = None,
        original_tool_call: dict[str, Any] | None = None,
        allowed_decisions: list[str] | None = None,
        resume_state: dict[str, Any] | None = None,
    ) -> Approval:
        approval = Approval(
            subject_type=subject_type,
            subject_id=subject_id,
            status="pending",
            payload=payload or {},
            turn_checkpoint=turn_checkpoint or {},
            original_tool_call=original_tool_call or {},
            allowed_decisions=allowed_decisions or ["approve", "edit", "reject", "respond"],
            resume_state=resume_state or {},
        )
        db.add(approval)
        db.flush()
        if self.events:
            self.events.emit(
                "approval.created",
                {"approval_id": str(approval.id), "subject_type": subject_type, "subject_id": subject_id},
            )
            self.events.audit("approval.create", "approval", target_id=str(approval.id), payload=payload or {})
        return approval

    def list_approvals(self, db: Session, *, status: str | None = None, limit: int = 100) -> list[Approval]:
        stmt = select(Approval).order_by(desc(Approval.created_at)).limit(max(1, min(limit, 500)))
        if status:
            stmt = stmt.where(Approval.status == status)
        return list(db.scalars(stmt).all())

    def resolve_approval(self, db: Session, approval_id: uuid.UUID, *, status: str) -> Approval:
        if status not in {"approved", "rejected", "cancelled"}:
            raise ValueError("approval status must be approved, rejected, or cancelled")
        approval = db.get(Approval, approval_id)
        if approval is None:
            raise KeyError(f"approval not found: {approval_id}")
        approval.status = status
        approval.resolved_at = datetime.now(UTC)
        if self.events:
            self.events.emit("approval.resolved", {"approval_id": str(approval.id), "status": status})
            self.events.audit("approval.resolve", "approval", target_id=str(approval.id), payload={"status": status})
        return approval

    def list_cron_jobs(self, db: Session, *, enabled: bool | None = None, limit: int = 100) -> list[CronJob]:
        stmt = select(CronJob).order_by(CronJob.name).limit(max(1, min(limit, 500)))
        if enabled is not None:
            stmt = stmt.where(CronJob.enabled.is_(enabled))
        return list(db.scalars(stmt).all())

    def upsert_cron_job(
        self,
        db: Session,
        *,
        name: str,
        cron_expr: str,
        timezone: str = "Asia/Hong_Kong",
        instruction: str = "",
        enabled: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> CronJob:
        if not croniter.is_valid(cron_expr):
            raise ValueError("invalid cron expression")
        job = db.scalar(select(CronJob).where(CronJob.name == name))
        if job is None:
            job = CronJob(name=name, cron_expr=cron_expr)
            db.add(job)
        job.cron_expr = cron_expr
        job.timezone = timezone
        job.instruction = instruction
        job.enabled = enabled
        job.metadata_json = metadata or {}
        job.next_run_at = compute_next_run(cron_expr, timezone) if enabled else None
        db.flush()
        if self.events:
            self.events.emit("cron.upsert", {"name": name, "enabled": enabled})
            self.events.audit("cron.upsert", "cron_job", target_id=name, payload={"enabled": enabled})
        return job

    def ensure_system_cron_job(
        self,
        db: Session,
        *,
        name: str,
        cron_expr: str,
        timezone: str,
        instruction: str,
        metadata: dict[str, Any],
        enabled: bool = True,
    ) -> CronJob:
        if not croniter.is_valid(cron_expr):
            raise ValueError("invalid cron expression")
        job = db.scalar(select(CronJob).where(CronJob.name == name))
        if job is None:
            job = CronJob(name=name, cron_expr=cron_expr)
            db.add(job)
            job.enabled = enabled
            job.next_run_at = compute_next_run(cron_expr, timezone) if enabled else None
        schedule_changed = job.cron_expr != cron_expr or job.timezone != timezone
        job.cron_expr = cron_expr
        job.timezone = timezone
        job.instruction = instruction
        job.metadata_json = metadata
        if job.enabled and (schedule_changed or job.next_run_at is None):
            job.next_run_at = compute_next_run(cron_expr, timezone)
        db.flush()
        if self.events:
            self.events.emit("cron.system.ensure", {"name": name, "enabled": job.enabled})
        return job

    def list_mcp_servers(self, db: Session, *, enabled: bool | None = None, limit: int = 100) -> list[MCPServer]:
        stmt = select(MCPServer).order_by(MCPServer.name).limit(max(1, min(limit, 500)))
        if enabled is not None:
            stmt = stmt.where(MCPServer.enabled.is_(enabled))
        return list(db.scalars(stmt).all())

    def upsert_mcp_server(
        self,
        db: Session,
        *,
        name: str,
        transport: str = "stdio",
        command: str = "",
        url: str = "",
        config: dict[str, Any] | None = None,
        enabled: bool = True,
        config_source: str = "manual",
        permission_policy: dict[str, Any] | None = None,
        session_mode: str = "transient",
        health_status: str = "unknown",
        last_imported_checksum: str = "",
    ) -> MCPServer:
        if transport not in {"stdio", "sse", "streamable_http", "http"}:
            raise ValueError("unsupported MCP transport")
        if session_mode not in {"transient", "managed"}:
            raise ValueError("unsupported MCP session_mode")
        if transport == "stdio" and not command:
            raise ValueError("stdio MCP servers require a command")
        if transport != "stdio" and not url:
            raise ValueError("network MCP servers require a url")
        server = db.scalar(select(MCPServer).where(MCPServer.name == name))
        if server is None:
            server = MCPServer(name=name)
            db.add(server)
        server.transport = transport
        server.command = command
        server.url = url
        server.config = config or {}
        server.config_source = config_source or "manual"
        server.permission_policy = permission_policy or {}
        server.session_mode = session_mode
        server.health_status = health_status
        server.last_imported_checksum = last_imported_checksum
        server.enabled = enabled
        server.status = "pending" if enabled else "disabled"
        if not enabled:
            server.last_error = ""
            server.tool_count = 0
            server.tools_cache = []
        db.flush()
        if self.events:
            self.events.emit("mcp.upsert", {"name": name, "transport": transport, "enabled": enabled})
            self.events.audit("mcp.upsert", "mcp_server", target_id=name, payload={"transport": transport, "enabled": enabled})
        return server

    def import_mcp_seed(self, db: Session, settings: Settings | None = None) -> dict[str, Any]:
        settings = settings or get_settings()
        path = settings.mcp_config_file.expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            return {"path": str(path), "imported": 0, "skipped": 0, "missing": True}
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        servers = loaded.get("servers") if isinstance(loaded, dict) else {}
        if not isinstance(servers, dict):
            raise ValueError("MCP seed file must contain a mapping at `servers`")
        imported = 0
        skipped = 0
        for name, raw_config in servers.items():
            if not isinstance(raw_config, dict):
                skipped += 1
                continue
            server_name = str(name)
            existing = db.scalar(select(MCPServer).where(MCPServer.name == server_name))
            checksum = hashlib.sha256(
                json.dumps(raw_config, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
            ).hexdigest()
            if existing is not None and not str(getattr(existing, "config_source", "")).startswith("yaml:"):
                skipped += 1
                continue
            if existing is not None and existing.last_imported_checksum == checksum:
                skipped += 1
                continue
            transport = str(raw_config.get("transport") or "stdio")
            command = str(raw_config.get("command") or "")
            url = str(raw_config.get("url") or "")
            config = {key: value for key, value in raw_config.items() if key not in {"transport", "command", "url", "enabled"}}
            tools = config.get("tools") if isinstance(config.get("tools"), dict) else {}
            permission_policy = tools if isinstance(tools, dict) else {}
            session_mode = str(raw_config.get("session_mode") or config.get("session_mode") or "transient")
            self.upsert_mcp_server(
                db,
                name=server_name,
                transport=transport,
                command=command,
                url=url,
                config=config,
                enabled=bool(raw_config.get("enabled", True)),
                config_source=f"yaml:{path}",
                permission_policy=permission_policy,
                session_mode=session_mode,
                health_status="unknown",
                last_imported_checksum=checksum,
            )
            imported += 1
        if self.events:
            self.events.emit("mcp.seed.imported", {"path": str(path), "imported": imported, "skipped": skipped})
        return {"path": str(path), "imported": imported, "skipped": skipped, "missing": False}

    def list_gateways(self, db: Session, *, kind: str | None = None, limit: int = 100) -> list[GatewayConnection]:
        stmt = select(GatewayConnection).order_by(GatewayConnection.name).limit(max(1, min(limit, 500)))
        if kind:
            stmt = stmt.where(GatewayConnection.kind == kind)
        return list(db.scalars(stmt).all())

    def upsert_gateway(
        self,
        db: Session,
        *,
        name: str,
        kind: str,
        endpoint: str = "",
        config: dict[str, Any] | None = None,
        enabled: bool = False,
        status: str | None = None,
    ) -> GatewayConnection:
        if kind not in {"weixin", "wecom", "telegram", "slack", "webhook", "local"}:
            raise ValueError("unsupported gateway kind")
        gateway = db.scalar(select(GatewayConnection).where(GatewayConnection.name == name))
        if gateway is None:
            gateway = GatewayConnection(name=name, kind=kind)
            db.add(gateway)
        gateway.kind = kind
        gateway.endpoint = endpoint
        gateway.config = config or {}
        gateway.enabled = enabled
        gateway.status = status or ("enabled" if enabled else "disabled")
        if not enabled:
            gateway.status = "disabled"
            gateway.last_error = ""
        db.flush()
        if self.events:
            self.events.emit("gateway.upsert", {"name": name, "kind": kind, "enabled": enabled})
            self.events.audit("gateway.upsert", "gateway", target_id=name, payload={"kind": kind, "enabled": enabled})
        return gateway
