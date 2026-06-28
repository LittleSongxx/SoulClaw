"""Gateway runtime for inbound IM/webhook messages and controlled outbound sends."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select

from backend.infra.db import session_scope
from backend.infra.events import RuntimeEventBus
from backend.infra.models import GatewayConnection
from backend.runtime.agent import AgentRuntime


@dataclass(frozen=True)
class InboundGatewayMessage:
    gateway_name: str
    external_user_id: str
    text: str
    channel_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OutboundGatewayMessage:
    gateway_name: str
    target_id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class GatewayRuntimeManager:
    def __init__(
        self,
        *,
        agent: AgentRuntime,
        events: RuntimeEventBus,
        http_timeout_seconds: float = 15.0,
    ) -> None:
        self.agent = agent
        self.events = events
        self.http_timeout_seconds = http_timeout_seconds

    def handle_inbound(self, message: InboundGatewayMessage) -> dict[str, Any]:
        with session_scope() as db:
            gateway = self._require_gateway(db, message.gateway_name)
            self._verify_signature(gateway, message)
            gateway.last_inbound_at = datetime.now(UTC)
            gateway.inbound_count = int(gateway.inbound_count or 0) + 1
            gateway.status = "online"
            session_id = self._session_id(gateway.name, message.channel_id, message.external_user_id)
            self.events.emit(
                "gateway.inbound",
                {
                    "gateway": gateway.name,
                    "kind": gateway.kind,
                    "external_user_id": message.external_user_id,
                    "channel_id": message.channel_id,
                },
                session_id=session_id,
            )
            try:
                result = self.agent.run_turn(db, message.text, session_id=session_id)
            except Exception as exc:  # noqa: BLE001
                gateway.failure_count = int(gateway.failure_count or 0) + 1
                gateway.last_error = str(exc)
                gateway.status = "failed"
                self.events.emit(
                    "gateway.inbound.failed",
                    {"gateway": gateway.name, "error": str(exc)},
                    severity="warning",
                    session_id=session_id,
                )
                raise
            return {
                "gateway": gateway.name,
                "session_id": session_id,
                "turn_id": result.turn_id,
                "answer": result.answer,
                "context": result.context,
            }

    def send(self, message: OutboundGatewayMessage) -> dict[str, Any]:
        with session_scope() as db:
            gateway = self._require_gateway(db, message.gateway_name)
            try:
                delivery = self._deliver(gateway, message)
            except Exception as exc:  # noqa: BLE001
                gateway.failure_count = int(gateway.failure_count or 0) + 1
                gateway.last_error = str(exc)
                gateway.status = "failed"
                self.events.emit(
                    "gateway.outbound.failed",
                    {"gateway": gateway.name, "target_id": message.target_id, "error": str(exc)},
                    severity="warning",
                )
                raise
            gateway.last_outbound_at = datetime.now(UTC)
            gateway.outbound_count = int(gateway.outbound_count or 0) + 1
            gateway.last_error = ""
            gateway.status = "online"
            self.events.emit("gateway.outbound.succeeded", {"gateway": gateway.name, "target_id": message.target_id})
            return delivery

    def _require_gateway(self, db, gateway_name: str) -> GatewayConnection:
        gateway = db.scalar(select(GatewayConnection).where(GatewayConnection.name == gateway_name))
        if gateway is None:
            raise KeyError(f"gateway not found: {gateway_name}")
        if not gateway.enabled:
            raise RuntimeError(f"gateway disabled: {gateway_name}")
        return gateway

    def _deliver(self, gateway: GatewayConnection, message: OutboundGatewayMessage) -> dict[str, Any]:
        if gateway.kind == "local":
            return {
                "mode": "local",
                "gateway": gateway.name,
                "target_id": message.target_id,
                "text": message.text,
                "metadata": message.metadata,
            }
        if gateway.kind in {"webhook", "slack"}:
            url = message.target_id or gateway.endpoint
            if not url.startswith(("http://", "https://")):
                raise ValueError("webhook/slack gateway requires an http target_id or endpoint")
            payload = {
                "text": message.text,
                "metadata": message.metadata,
                "gateway": gateway.name,
                "kind": gateway.kind,
            }
            with httpx.Client(timeout=self.http_timeout_seconds) as client:
                response = client.post(url, json=payload)
                response.raise_for_status()
            return {"mode": "http_post", "status_code": response.status_code}
        raise NotImplementedError(f"gateway kind '{gateway.kind}' is configured but no outbound adapter is active")

    @staticmethod
    def _verify_signature(gateway: GatewayConnection, message: InboundGatewayMessage) -> None:
        config = gateway.config or {}
        secret = str(config.get("secret") or "")
        if not secret:
            return
        signature = str(message.metadata.get("signature") or message.metadata.get("x_soulclaw_signature") or "")
        expected = hmac.new(secret.encode("utf-8"), message.text.encode("utf-8"), hashlib.sha256).hexdigest()
        if signature.startswith("sha256="):
            signature = signature.removeprefix("sha256=")
        if not hmac.compare_digest(signature, expected):
            raise RuntimeError("invalid gateway signature")

    @staticmethod
    def _session_id(gateway_name: str, channel_id: str, external_user_id: str) -> str:
        parts = [gateway_name, channel_id or "default", external_user_id or "anonymous"]
        return "gateway:" + ":".join(part.replace(":", "_") for part in parts)
