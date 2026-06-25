"""Layered long-term memory repository and retriever."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session

from backend.infra.config import Settings, get_settings
from backend.infra.events import RuntimeEventBus
from backend.infra.models import Memory, MemoryConflict, MemoryProbe
from backend.infra.qdrant_index import IndexDocument, QdrantHybridIndex

L1_KINDS = {"control_axiom", "pinned_fact"}
L2_KINDS = {"episodic", "user_fact", "agent_note", "error_signal"}
L3_KINDS = {"semantic", "skill_trace", "project_knowledge"}


class MemoryService:
    def __init__(
        self,
        settings: Settings | None = None,
        qdrant: QdrantHybridIndex | None = None,
        events: RuntimeEventBus | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.qdrant = qdrant
        self.events = events

    def create(
        self,
        db: Session,
        *,
        kind: str,
        content: str,
        source: str = "explicit",
        pinned: bool = False,
        importance: float = 0.5,
        confidence: float = 0.5,
        stability: float = 0.5,
        supersedes_id: uuid.UUID | None = None,
        source_turn_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Memory:
        memory = Memory(
            kind=kind,
            content=content,
            source=source,
            pinned=pinned,
            importance=self._clamp(importance),
            confidence=self._clamp(confidence),
            stability=self._clamp(stability),
            supersedes_id=supersedes_id,
            source_turn_id=source_turn_id,
            metadata_json=metadata or {},
        )
        db.add(memory)
        if supersedes_id is not None:
            previous = db.get(Memory, supersedes_id)
            if previous is not None:
                previous.archived = True
        db.flush()
        self._index([memory])
        if self.events:
            self.events.emit("memory.create", {"memory_id": str(memory.id), "kind": kind, "source": source})
        return memory

    def list(self, db: Session, *, kind: str | None = None, include_archived: bool = False, limit: int = 100) -> list[Memory]:
        limit = max(1, min(limit, 500))
        stmt = select(Memory).order_by(desc(Memory.updated_at)).limit(limit)
        if kind:
            stmt = stmt.where(Memory.kind == kind)
        if not include_archived:
            stmt = stmt.where(Memory.archived.is_(False))
        return list(db.scalars(stmt).all())

    def search(self, db: Session, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 50))
        records: dict[str, dict[str, Any]] = {}
        if query.strip():
            pattern = f"%{query.strip()}%"
            stmt = (
                select(Memory)
                .where(Memory.archived.is_(False), Memory.content.ilike(pattern))
                .order_by(desc(Memory.importance), desc(Memory.updated_at))
                .limit(limit)
            )
        else:
            stmt = (
                select(Memory)
                .where(Memory.archived.is_(False))
                .order_by(desc(Memory.pinned), desc(Memory.importance), desc(Memory.updated_at))
                .limit(limit)
            )
        for memory in db.scalars(stmt).all():
            records[str(memory.id)] = {"score": 1.0, "source": "postgres", "memory": memory}

        if self.qdrant is not None and query.strip():
            hits = self.qdrant.search(self.settings.qdrant_memory_collection, query, limit=limit)
            ids = [
                item.get("payload", {}).get("memory_id")
                for item in hits
                if item.get("payload", {}).get("memory_id")
            ]
            parsed_ids: list[uuid.UUID] = []
            for raw in ids:
                try:
                    parsed_ids.append(uuid.UUID(str(raw)))
                except ValueError:
                    continue
            if parsed_ids:
                for memory in db.scalars(select(Memory).where(Memory.id.in_(parsed_ids), Memory.archived.is_(False))).all():
                    hit = next((item for item in hits if item.get("payload", {}).get("memory_id") == str(memory.id)), {})
                    records.setdefault(str(memory.id), {"score": hit.get("score"), "source": "qdrant", "memory": memory})
        return list(records.values())[:limit]

    def resident_context(self, db: Session, query: str, *, dynamic_limit: int = 8) -> dict[str, list[Memory]]:
        resident_stmt = (
            select(Memory)
            .where(
                Memory.archived.is_(False),
                or_(Memory.pinned.is_(True), Memory.kind.in_(sorted(L1_KINDS))),
            )
            .order_by(desc(Memory.pinned), desc(Memory.importance), desc(Memory.updated_at))
            .limit(20)
        )
        resident = list(db.scalars(resident_stmt).all())
        dynamic = [item["memory"] for item in self.search(db, query, limit=dynamic_limit)]
        seen = {memory.id for memory in resident}
        dynamic = [memory for memory in dynamic if memory.id not in seen]
        return {"resident": resident, "dynamic": dynamic}

    def supersede(self, db: Session, memory_id: uuid.UUID, replacement: dict[str, Any]) -> Memory:
        previous = db.get(Memory, memory_id)
        if previous is None:
            raise KeyError(f"memory not found: {memory_id}")
        previous.archived = True
        return self.create(
            db,
            kind=replacement.get("kind") or previous.kind,
            content=replacement["content"],
            source=replacement.get("source", "supersede"),
            pinned=bool(replacement.get("pinned", previous.pinned)),
            importance=float(replacement.get("importance", previous.importance)),
            confidence=float(replacement.get("confidence", previous.confidence)),
            stability=float(replacement.get("stability", previous.stability)),
            supersedes_id=memory_id,
            metadata=replacement.get("metadata") or {"supersedes": str(memory_id)},
        )

    def list_conflicts(self, db: Session, *, status: str | None = "open", limit: int = 100) -> list[MemoryConflict]:
        stmt = select(MemoryConflict).order_by(desc(MemoryConflict.created_at)).limit(max(1, min(limit, 500)))
        if status:
            stmt = stmt.where(MemoryConflict.status == status)
        return list(db.scalars(stmt).all())

    def create_probe(self, db: Session, *, question: str, expected: str = "") -> MemoryProbe:
        probe = MemoryProbe(question=question, expected=expected, status="active", last_result={})
        db.add(probe)
        db.flush()
        return probe

    def list_probes(self, db: Session, *, status: str | None = None, limit: int = 100) -> list[MemoryProbe]:
        stmt = select(MemoryProbe).order_by(desc(MemoryProbe.created_at)).limit(max(1, min(limit, 500)))
        if status:
            stmt = stmt.where(MemoryProbe.status == status)
        return list(db.scalars(stmt).all())

    def mark_verified(self, db: Session, memory_id: uuid.UUID, *, confidence_delta: float = 0.05) -> Memory:
        memory = db.get(Memory, memory_id)
        if memory is None:
            raise KeyError(f"memory not found: {memory_id}")
        memory.last_verified_at = datetime.now(UTC)
        memory.confidence = self._clamp(memory.confidence + confidence_delta)
        memory.stability = self._clamp(memory.stability + confidence_delta / 2)
        return memory

    def _index(self, memories: list[Memory]) -> None:
        if self.qdrant is None:
            return
        docs = [
            IndexDocument(
                key=str(memory.id),
                text=memory.content,
                payload={
                    "memory_id": str(memory.id),
                    "kind": memory.kind,
                    "source": memory.source,
                    "importance": memory.importance,
                    "confidence": memory.confidence,
                    "stability": memory.stability,
                    "pinned": memory.pinned,
                },
            )
            for memory in memories
        ]
        self.qdrant.upsert_documents(self.settings.qdrant_memory_collection, docs)

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, float(value)))

