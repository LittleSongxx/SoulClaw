"""Conversation history and rolling session context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from backend.infra.events import RuntimeEventBus
from backend.infra.models import SessionMessage, SessionSummary
from backend.runtime.llm import OpenAICompatibleClient


@dataclass(frozen=True)
class SessionContext:
    summary: str
    messages: list[SessionMessage]
    total_messages: int


class ConversationService:
    def __init__(
        self,
        *,
        events: RuntimeEventBus | None = None,
        summary_threshold: int = 24,
        recent_limit: int = 12,
    ) -> None:
        self.events = events
        self.summary_threshold = summary_threshold
        self.recent_limit = recent_limit

    def record_user_message(
        self,
        db: Session,
        *,
        session_id: str,
        turn_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> SessionMessage:
        return self._record(db, session_id=session_id, turn_id=turn_id, role="user", content=content, metadata=metadata)

    def record_assistant_message(
        self,
        db: Session,
        *,
        session_id: str,
        turn_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> SessionMessage:
        return self._record(
            db,
            session_id=session_id,
            turn_id=turn_id,
            role="assistant",
            content=content,
            metadata=metadata,
        )

    def record_tool_message(
        self,
        db: Session,
        *,
        session_id: str,
        turn_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> SessionMessage:
        return self._record(db, session_id=session_id, turn_id=turn_id, role="tool", content=content, metadata=metadata)

    def recent_context(self, db: Session, session_id: str, *, limit: int | None = None) -> SessionContext:
        limit = max(1, min(limit or self.recent_limit, 50))
        total = db.scalar(
            select(func.count()).select_from(SessionMessage).where(SessionMessage.session_id == session_id)
        ) or 0
        summary_record = db.scalar(select(SessionSummary).where(SessionSummary.session_id == session_id))
        messages = list(
            db.scalars(
                select(SessionMessage)
                .where(SessionMessage.session_id == session_id)
                .order_by(desc(SessionMessage.created_at))
                .limit(limit)
            ).all()
        )
        messages.reverse()
        return SessionContext(
            summary=summary_record.summary if summary_record else "",
            messages=messages,
            total_messages=int(total),
        )

    def list_sessions(self, db: Session, *, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        last_message_at = func.max(SessionMessage.created_at).label("last_message_at")
        message_count = func.count(SessionMessage.id).label("message_count")
        rows = db.execute(
            select(
                SessionMessage.session_id,
                last_message_at,
                message_count,
            )
            .group_by(SessionMessage.session_id)
            .order_by(desc(last_message_at))
            .limit(limit)
        ).all()
        return [
            {
                "session_id": row.session_id,
                "last_message_at": row.last_message_at,
                "message_count": int(row.message_count or 0),
            }
            for row in rows
        ]

    def get_summary(self, db: Session, session_id: str) -> SessionSummary | None:
        return db.scalar(select(SessionSummary).where(SessionSummary.session_id == session_id))

    def list_messages(self, db: Session, session_id: str, *, limit: int = 100) -> list[SessionMessage]:
        limit = max(1, min(limit, 500))
        messages = list(
            db.scalars(
                select(SessionMessage)
                .where(SessionMessage.session_id == session_id)
                .order_by(desc(SessionMessage.created_at))
                .limit(limit)
            ).all()
        )
        messages.reverse()
        return messages

    def update_summary_if_needed(
        self,
        db: Session,
        *,
        session_id: str,
        llm: OpenAICompatibleClient | None,
    ) -> SessionSummary | None:
        if llm is None or not llm.configured:
            return None
        total = db.scalar(
            select(func.count()).select_from(SessionMessage).where(SessionMessage.session_id == session_id)
        ) or 0
        if total < self.summary_threshold:
            return None
        summary = db.scalar(select(SessionSummary).where(SessionSummary.session_id == session_id))
        summarized = summary.summarized_message_count if summary else 0
        if int(total) - int(summarized or 0) < self.summary_threshold:
            return None
        messages = self.list_messages(db, session_id, limit=80)
        transcript = "\n".join(f"{item.role}: {item.content}" for item in messages[-80:])
        prompt = [
            {
                "role": "system",
                "content": "Summarize this ZLAgent session for future short-term context. Keep durable facts, goals, decisions, and unresolved threads. Be concise.",
            },
            {"role": "user", "content": transcript},
        ]
        try:
            response = llm.complete(messages=prompt, tools=None, temperature=0.1)
        except Exception as exc:  # noqa: BLE001
            if self.events:
                self.events.emit(
                    "conversation.summary.failed",
                    {"session_id": session_id, "error": str(exc)},
                    severity="warning",
                    session_id=session_id,
                )
            return None
        if summary is None:
            summary = SessionSummary(session_id=session_id)
            db.add(summary)
        summary.summary = response.content.strip()
        summary.summarized_message_count = int(total)
        summary.metadata_json = {"message_count": int(total)}
        if self.events:
            self.events.emit(
                "conversation.summary.updated",
                {"session_id": session_id, "message_count": int(total)},
                session_id=session_id,
            )
        return summary

    def _record(
        self,
        db: Session,
        *,
        session_id: str,
        turn_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> SessionMessage:
        message = SessionMessage(
            session_id=session_id,
            turn_id=turn_id,
            role=role,
            content=content,
            metadata_json=metadata or {},
        )
        db.add(message)
        db.flush()
        if self.events:
            self.events.emit(
                "conversation.message.recorded",
                {"role": role, "message_id": str(message.id)},
                session_id=session_id,
                turn_id=turn_id,
            )
        return message
