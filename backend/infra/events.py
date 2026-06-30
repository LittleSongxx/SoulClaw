"""Runtime and audit event sinks."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .db import session_scope
from .models import AuditEvent, RuntimeEvent
from .trace import current_request_id, current_trace_id


class RuntimeEventBus:
    """Small durable event bus backed by Postgres.

    This is intentionally synchronous for the first platform cut: event writes
    are short, auditable, and easy to reason about. Higher-volume streams can
    later add a Redis fanout without changing the domain surface.
    """

    def __init__(self) -> None:
        self._bound_session: ContextVar[Session | None] = ContextVar(
            f"soulclaw_event_session_{id(self)}",
            default=None,
        )

    @contextmanager
    def bind_session(self, db: Session) -> Iterator[None]:
        """Persist events through the caller's active transaction."""

        token = self._bound_session.set(db)
        try:
            yield
        finally:
            try:
                self._bound_session.reset(token)
            except ValueError:
                self._bound_session.set(None)

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
            event = RuntimeEvent(
                event_type=event_type,
                severity=severity,
                session_id=session_id,
                turn_id=turn_id,
                payload=_jsonable(payload),
                trace_id=current_trace_id(),
                request_id=current_request_id(),
            )
            bound = self._bound_session.get()
            if bound is not None:
                bound.add(event)
            else:
                with session_scope() as db:
                    db.add(event)
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
            event = AuditEvent(
                actor_id=actor_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                payload=_jsonable(payload),
                trace_id=current_trace_id(),
                request_id=current_request_id(),
            )
            bound = self._bound_session.get()
            if bound is not None:
                bound.add(event)
            else:
                with session_scope() as db:
                    db.add(event)
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


def _jsonable(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    try:
        encoded = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001
        return {"repr": repr(payload)}
    return encoded if isinstance(encoded, dict) else {"value": encoded}
