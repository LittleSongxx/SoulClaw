"""Runtime and audit event sinks."""

from __future__ import annotations

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import desc, select

from .db import session_scope
from .models import AuditEvent, RuntimeEvent


class RuntimeEventBus:
    """Small durable event bus backed by Postgres.

    This is intentionally synchronous for the first platform cut: event writes
    are short, auditable, and easy to reason about. Higher-volume streams can
    later add a Redis fanout without changing the domain surface.
    """

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        severity: str = "info",
        session_id: str = "",
        turn_id: str = "",
    ) -> None:
        try:
            with session_scope() as db:
                db.add(
                    RuntimeEvent(
                        event_type=event_type,
                        severity=severity,
                        session_id=session_id,
                        turn_id=turn_id,
                        payload=payload or {},
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[events] failed to persist runtime event {}: {}", event_type, exc)

    def audit(
        self,
        action: str,
        target_type: str,
        *,
        actor_id: uuid.UUID | None = None,
        target_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        try:
            with session_scope() as db:
                db.add(
                    AuditEvent(
                        actor_id=actor_id,
                        action=action,
                        target_type=target_type,
                        target_id=target_id,
                        payload=payload or {},
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[events] failed to persist audit event {}: {}", action, exc)

    def list_runtime_events(self, limit: int = 100, event_type: str | None = None) -> list[RuntimeEvent]:
        limit = max(1, min(limit, 500))
        with session_scope() as db:
            stmt = select(RuntimeEvent).order_by(desc(RuntimeEvent.created_at)).limit(limit)
            if event_type:
                stmt = stmt.where(RuntimeEvent.event_type == event_type)
            return list(db.scalars(stmt).all())

    def list_audit_events(self, limit: int = 100) -> list[AuditEvent]:
        limit = max(1, min(limit, 500))
        with session_scope() as db:
            stmt = select(AuditEvent).order_by(desc(AuditEvent.created_at)).limit(limit)
            return list(db.scalars(stmt).all())

