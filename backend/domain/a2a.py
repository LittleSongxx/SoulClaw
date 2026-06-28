"""A2A connection, task, event, and artifact repository helpers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session

from backend.infra.events import RuntimeEventBus
from backend.infra.models import A2AAgentConnection, A2AArtifact, A2AEvent, A2ATask


class A2AService:
    def __init__(self, *, events: RuntimeEventBus | None = None) -> None:
        self.events = events

    def upsert_connection(
        self,
        db: Session,
        *,
        name: str,
        kind: str = "a2a",
        endpoint: str = "",
        rpc_url: str = "",
        agent_card: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
        enabled: bool = True,
        status: str | None = None,
        capabilities: list[str] | None = None,
        skills: list[dict[str, Any]] | None = None,
    ) -> A2AAgentConnection:
        name = str(name or "").strip()
        if not name:
            raise ValueError("A2A connection name is required")
        if kind not in {"a2a", "weaver"}:
            raise ValueError("unsupported A2A connection kind")
        if not endpoint and not rpc_url:
            raise ValueError("A2A connection requires endpoint or rpc_url")
        connection = db.scalar(select(A2AAgentConnection).where(A2AAgentConnection.name == name))
        if connection is None:
            connection = A2AAgentConnection(name=name)
            db.add(connection)
        connection.kind = kind
        connection.endpoint = endpoint.rstrip("/")
        connection.rpc_url = rpc_url
        connection.agent_card = agent_card or connection.agent_card or {}
        connection.config = config or {}
        connection.enabled = enabled
        connection.status = status or ("pending" if enabled else "disabled")
        connection.capabilities = capabilities or _capabilities_from_card(connection.agent_card)
        connection.skills = skills or _skills_from_card(connection.agent_card)
        if not enabled:
            connection.status = "disabled"
            connection.last_error = ""
        db.flush()
        if self.events:
            self.events.emit("a2a.connection.upserted", {"name": name, "kind": kind, "enabled": enabled})
            self.events.audit("a2a.connection.upsert", "a2a_connection", target_id=name, payload={"kind": kind, "enabled": enabled})
        return connection

    def update_discovery(
        self,
        db: Session,
        connection: A2AAgentConnection,
        *,
        agent_card: dict[str, Any],
        rpc_url: str = "",
        status: str = "online",
        error: str = "",
    ) -> A2AAgentConnection:
        connection.agent_card = agent_card
        connection.rpc_url = rpc_url or connection.rpc_url
        connection.capabilities = _capabilities_from_card(agent_card)
        connection.skills = _skills_from_card(agent_card)
        connection.status = status
        connection.last_error = error
        connection.last_discovered_at = datetime.now(UTC)
        db.flush()
        if self.events:
            self.events.emit(
                "a2a.connection.discovered",
                {"name": connection.name, "status": status, "skills": len(connection.skills or [])},
                severity="warning" if error else "info",
            )
        return connection

    def list_connections(
        self,
        db: Session,
        *,
        enabled: bool | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[A2AAgentConnection]:
        stmt = select(A2AAgentConnection).order_by(A2AAgentConnection.name).limit(max(1, min(limit, 500)))
        if enabled is not None:
            stmt = stmt.where(A2AAgentConnection.enabled.is_(enabled))
        if kind:
            stmt = stmt.where(A2AAgentConnection.kind == kind)
        return list(db.scalars(stmt).all())

    def get_connection(self, db: Session, name: str) -> A2AAgentConnection | None:
        return db.scalar(select(A2AAgentConnection).where(A2AAgentConnection.name == name))

    def create_task(
        self,
        db: Session,
        *,
        connection_name: str,
        capability: str,
        input_text: str,
        context_id: str = "",
        metadata: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> A2ATask:
        task = A2ATask(
            task_id=task_id or f"a2a_task_{uuid.uuid4().hex}",
            connection_name=connection_name,
            capability=capability,
            context_id=context_id or f"a2a_ctx_{uuid.uuid4().hex}",
            input_text=input_text,
            status="submitted",
            metadata_json=metadata or {},
        )
        db.add(task)
        db.flush()
        self.add_event(db, task.task_id, "task.submitted", {"connection": connection_name, "capability": capability})
        if self.events:
            self.events.emit(
                "a2a.task.submitted",
                {"task_id": task.task_id, "connection": connection_name, "capability": capability},
            )
            self.events.audit("a2a.task.create", "a2a_task", target_id=task.task_id, payload={"connection": connection_name, "capability": capability})
        return task

    def mark_task(
        self,
        db: Session,
        task: A2ATask,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str = "",
        remote_task_id: str = "",
        remote_context_id: str = "",
    ) -> A2ATask:
        previous = task.status
        task.status = status
        if remote_task_id:
            task.remote_task_id = remote_task_id
        if remote_context_id:
            task.remote_context_id = remote_context_id
        if result is not None:
            task.result = result
        task.error = error
        if previous == "submitted" and status in {"working", "input-required"} and task.started_at is None:
            task.started_at = datetime.now(UTC)
        if status in {"completed", "failed", "canceled", "rejected"}:
            task.finished_at = datetime.now(UTC)
        db.flush()
        self.add_event(db, task.task_id, f"task.{status}", {"previous_status": previous, "error": error, "result": result or {}})
        if self.events:
            self.events.emit(
                "a2a.task.status",
                {"task_id": task.task_id, "status": status, "previous_status": previous},
                severity="warning" if status in {"failed", "rejected"} else "info",
            )
        return task

    def get_task(self, db: Session, task_id: str) -> A2ATask | None:
        return db.scalar(select(A2ATask).where(A2ATask.task_id == task_id))

    def get_task_by_any_id(self, db: Session, task_id: str) -> A2ATask | None:
        task_id = str(task_id or "").strip()
        if not task_id:
            return None
        return db.scalar(
            select(A2ATask).where(
                or_(
                    A2ATask.task_id == task_id,
                    A2ATask.remote_task_id == task_id,
                    A2ATask.remote_context_id == task_id,
                )
            )
        )

    def list_tasks(
        self,
        db: Session,
        *,
        status: str | None = None,
        connection_name: str | None = None,
        limit: int = 100,
    ) -> list[A2ATask]:
        stmt = select(A2ATask).order_by(desc(A2ATask.created_at)).limit(max(1, min(limit, 500)))
        if status:
            stmt = stmt.where(A2ATask.status == status)
        if connection_name:
            stmt = stmt.where(A2ATask.connection_name == connection_name)
        return list(db.scalars(stmt).all())

    def add_artifact(
        self,
        db: Session,
        task_id: str,
        *,
        artifact_id: str | None = None,
        name: str = "",
        mime_type: str = "text/plain",
        content: str = "",
        uri: str = "",
        parts: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> A2AArtifact:
        artifact = A2AArtifact(
            task_id=task_id,
            artifact_id=artifact_id or f"artifact_{uuid.uuid4().hex}",
            name=name,
            mime_type=mime_type,
            content=content,
            uri=uri,
            parts=parts or ([{"kind": "text", "text": content}] if content else []),
            metadata_json=metadata or {},
        )
        db.add(artifact)
        db.flush()
        self.add_event(
            db,
            task_id,
            "artifact.updated",
            {"artifact_id": artifact.artifact_id, "name": artifact.name, "mime_type": artifact.mime_type},
        )
        return artifact

    def list_artifacts(self, db: Session, task_id: str) -> list[A2AArtifact]:
        stmt = select(A2AArtifact).where(A2AArtifact.task_id == task_id).order_by(A2AArtifact.created_at)
        return list(db.scalars(stmt).all())

    def add_event(self, db: Session, task_id: str, event_type: str, payload: dict[str, Any] | None = None) -> A2AEvent:
        latest = db.scalar(
            select(A2AEvent)
            .where(A2AEvent.task_id == task_id)
            .order_by(desc(A2AEvent.sequence))
            .limit(1)
        )
        event = A2AEvent(
            task_id=task_id,
            event_type=event_type,
            sequence=(int(latest.sequence) + 1 if latest is not None else 1),
            payload=payload or {},
        )
        db.add(event)
        db.flush()
        return event

    def list_events(self, db: Session, task_id: str, *, after_sequence: int = 0, limit: int = 200) -> list[A2AEvent]:
        stmt = (
            select(A2AEvent)
            .where(A2AEvent.task_id == task_id, A2AEvent.sequence > after_sequence)
            .order_by(A2AEvent.sequence)
            .limit(max(1, min(limit, 1000)))
        )
        return list(db.scalars(stmt).all())


def _capabilities_from_card(card: dict[str, Any]) -> list[str]:
    capabilities: set[str] = set()
    raw_caps = card.get("capabilities") if isinstance(card, dict) else {}
    if isinstance(raw_caps, dict):
        for key, value in raw_caps.items():
            if bool(value):
                capabilities.add(str(key))
    for skill in _skills_from_card(card):
        for tag in skill.get("tags", []) or []:
            capabilities.add(str(tag))
        if skill.get("id"):
            capabilities.add(str(skill["id"]))
        if skill.get("name"):
            capabilities.add(str(skill["name"]).lower().replace(" ", "-"))
    return sorted(capabilities)


def _skills_from_card(card: dict[str, Any]) -> list[dict[str, Any]]:
    raw = card.get("skills") if isinstance(card, dict) else []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]
