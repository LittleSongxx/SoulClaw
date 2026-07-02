"""Layered long-term memory repository and retriever."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, or_, select, text
from sqlalchemy.orm import Session

from backend.domain.vector import KnowledgeVectorService
from backend.infra.config import Settings, get_settings
from backend.infra.events import RuntimeEventBus
from backend.infra.models import (
    EvolutionProposal,
    Memory,
    MemoryConflict,
    MemoryHistory,
    MemoryProbe,
)

L1_KINDS = {"control_axiom", "pinned_fact"}
L2_KINDS = {"episodic", "user_fact", "agent_note", "error_signal"}
L3_KINDS = {"semantic", "skill_trace", "project_knowledge"}
MEMORY_DEFAULT_BUDGET_CHARS = 12000
MEMORY_EXPIRED_STATUSES = {"archived", "superseded"}


class MemoryService:
    def __init__(
        self,
        settings: Settings | None = None,
        events: RuntimeEventBus | None = None,
        vector: KnowledgeVectorService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.events = events
        self.vector = vector

    def _create_applied_memory(
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
        now = datetime.now(UTC)
        metadata = metadata or {}
        valid_to = self._parse_datetime(metadata.get("valid_to") or metadata.get("expires_at"))
        memory = Memory(
            kind=kind,
            content=content,
            source=source,
            status="active",
            pinned=pinned,
            importance=self._clamp(importance),
            confidence=self._clamp(confidence),
            stability=self._clamp(stability),
            supersedes_id=supersedes_id,
            source_turn_id=source_turn_id,
            valid_to=valid_to,
            provenance={
                "source": source,
                "created_at": now.isoformat(),
                **(metadata.get("provenance", {}) if isinstance(metadata.get("provenance"), dict) else {}),
            },
            metadata_json={
                **metadata,
                "access": self._memory_access(metadata),
                "embedding": metadata.get("embedding") if isinstance(metadata.get("embedding"), dict) else {"status": "reserved"},
            },
        )
        db.add(memory)
        if supersedes_id is not None:
            previous = db.get(Memory, supersedes_id)
            if previous is not None:
                self._record_history(db, previous, action="superseded_by_create", before=self._snapshot(previous), actor=source)
                previous.archived = True
                previous.status = "superseded"
        db.flush()
        self._detect_conflicts(db, memory)
        self._record_history(db, memory, action="create", after=self._snapshot(memory), actor=source)
        self.refresh_fts(db)
        if self.vector is not None:
            try:
                self.vector.upsert_memories(db, [memory], strict=False)
            except Exception as exc:  # noqa: BLE001
                if self.events:
                    self.events.emit("memory.vector.refresh_failed", {"memory_id": str(memory.id), "error": str(exc)}, severity="warning")
        if self.events:
            self.events.emit("memory.create", {"memory_id": str(memory.id), "kind": kind, "source": source})
            self.events.audit("memory.create", "memory", target_id=str(memory.id), payload={"kind": kind, "source": source})
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
        query = query.strip()
        fts_hits = {item["memory_id"]: item for item in self._fts_search(db, query, limit=max(limit * 4, 20))}
        candidates: dict[str, Memory] = {}
        if fts_hits:
            try:
                ids = [uuid.UUID(item) for item in fts_hits]
                for memory in db.scalars(select(Memory).where(Memory.id.in_(ids))).all():
                    candidates[str(memory.id)] = memory
            except Exception:  # noqa: BLE001
                pass
        for memory in self._structured_candidates(db, query=query, limit=max(limit * 4, 20)):
            candidates[str(memory.id)] = memory
        vector_hits: dict[str, dict[str, Any]] = {}
        if self.vector is not None and query:
            try:
                for hit in self.vector.search(db, query=query, source_type="memory", limit=max(limit * 4, 20)):
                    memory_id = str(hit.get("source_id") or "")
                    if not memory_id:
                        continue
                    vector_hits[memory_id] = hit
                if vector_hits:
                    ids = [uuid.UUID(item) for item in vector_hits]
                    for memory in db.scalars(select(Memory).where(Memory.id.in_(ids))).all():
                        candidates[str(memory.id)] = memory
            except Exception as exc:  # noqa: BLE001
                if self.events:
                    self.events.emit("memory.vector.search_failed", {"query": query, "error": str(exc)}, severity="warning")

        now = datetime.now(UTC)
        ranked: list[dict[str, Any]] = []
        for memory_id, memory in candidates.items():
            if self._is_inactive(memory, now=now):
                continue
            fts_hit = fts_hits.get(memory_id, {})
            score, reasons = self._score_memory(memory, query=query, fts_hit=fts_hit, now=now)
            if query and score <= 0.0:
                if memory_id not in vector_hits:
                    continue
            vector_hit = vector_hits.get(memory_id, {})
            vector_score = float(vector_hit.get("vector_score") or 0.0)
            hybrid_score = vector_score * 0.7 + score * 0.3 if vector_hit else score
            ranked.append(
                {
                    "score": round(hybrid_score, 4),
                    "hybrid_score": round(hybrid_score, 4),
                    "vector_score": round(vector_score, 4),
                    "fts_score": round(score, 4),
                    "chunk_key": vector_hit.get("chunk_key") or "",
                    "source": "memory_hybrid" if vector_hit and fts_hit else ("memory_vector" if vector_hit else ("memory_fts" if fts_hit else "memory_index")),
                    "memory": memory,
                    "match_reasons": [*reasons, *(["vector"] if vector_hit else [])],
                    "snippet": str(fts_hit.get("snippet") or self._snippet(memory.content, query)),
                }
            )
        ranked.sort(key=lambda item: item["hybrid_score"], reverse=True)
        return self._mmr_dedup(ranked, limit=limit)

    def get(self, db: Session, memory_id: uuid.UUID) -> Memory | None:
        return db.get(Memory, memory_id)

    def file_context(self, *, limit_chars: int = 6000) -> str:
        del limit_chars
        return ""

    def resident_context(self, db: Session, query: str, *, dynamic_limit: int = 8) -> dict[str, list[Memory]]:
        now = datetime.now(UTC)
        resident_stmt = (
            select(Memory)
            .where(
                Memory.archived.is_(False),
                Memory.status.not_in(sorted(MEMORY_EXPIRED_STATUSES)),
                or_(Memory.pinned.is_(True), Memory.kind.in_(sorted(L1_KINDS))),
            )
            .order_by(desc(Memory.pinned), desc(Memory.importance), desc(Memory.updated_at))
            .limit(20)
        )
        resident = [item for item in db.scalars(resident_stmt).all() if not self._is_inactive(item, now=now)]
        dynamic = [item["memory"] for item in self.search(db, query, limit=dynamic_limit)]
        seen = {memory.id for memory in resident}
        dynamic = [memory for memory in dynamic if memory.id not in seen]
        return {"resident": resident, "dynamic": dynamic}

    def supersede(self, db: Session, memory_id: uuid.UUID, replacement: dict[str, Any]) -> Memory:
        previous = db.get(Memory, memory_id)
        if previous is None:
            raise KeyError(f"memory not found: {memory_id}")
        before = self._snapshot(previous)
        previous.archived = True
        previous.status = "superseded"
        memory = self._create_applied_memory(
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
        previous.superseded_by = memory.id
        if self.vector is not None:
            try:
                self.vector.mark_stale(db, source_type="memory", source_id=str(memory_id))
                self.vector.upsert_memories(db, [memory], strict=False)
            except Exception as exc:  # noqa: BLE001
                if self.events:
                    self.events.emit("memory.vector.supersede_failed", {"memory_id": str(memory_id), "error": str(exc)}, severity="warning")
        self._record_history(db, previous, action="supersede", before=before, after=self._snapshot(previous), actor=str(replacement.get("source") or "supersede"))
        self.refresh_fts(db)
        return memory

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
        before = self._snapshot(memory)
        memory.last_verified_at = datetime.now(UTC)
        memory.confidence = self._clamp(memory.confidence + confidence_delta)
        memory.stability = self._clamp(memory.stability + confidence_delta / 2)
        self._record_history(db, memory, action="verify", before=before, after=self._snapshot(memory), actor="memory-service")
        if self.events:
            self.events.audit("memory.verify", "memory", target_id=str(memory_id), payload={"confidence_delta": confidence_delta})
        return memory

    def archive(self, db: Session, memory_id: uuid.UUID) -> Memory:
        memory = db.get(Memory, memory_id)
        if memory is None:
            raise KeyError(f"memory not found: {memory_id}")
        before = self._snapshot(memory)
        memory.archived = True
        memory.status = "archived"
        self._record_history(db, memory, action="archive", before=before, after=self._snapshot(memory), actor="memory-service")
        self.refresh_fts(db)
        if self.vector is not None:
            try:
                self.vector.mark_stale(db, source_type="memory", source_id=str(memory_id))
            except Exception as exc:  # noqa: BLE001
                if self.events:
                    self.events.emit("memory.vector.archive_failed", {"memory_id": str(memory_id), "error": str(exc)}, severity="warning")
        if self.events:
            self.events.emit("memory.archive", {"memory_id": str(memory_id)})
            self.events.audit("memory.archive", "memory", target_id=str(memory_id))
        return memory

    def restore(self, db: Session, history_id: uuid.UUID, *, actor: str = "admin") -> Memory:
        history = db.get(MemoryHistory, history_id)
        if history is None:
            raise KeyError(f"memory history not found: {history_id}")
        snapshot = history.before_snapshot.get("memory") or history.before_snapshot or history.after_snapshot.get("memory")
        if not isinstance(snapshot, dict) or not snapshot.get("id"):
            raise ValueError("memory history has no restorable snapshot")
        memory_id = uuid.UUID(str(snapshot["id"]))
        memory = db.get(Memory, memory_id)
        if memory is None:
            memory = Memory(id=memory_id, kind=str(snapshot.get("kind") or "agent_note"), content=str(snapshot.get("content") or ""))
            db.add(memory)
        before = self._snapshot(memory)
        self._apply_snapshot(memory, snapshot)
        self._record_history(db, memory, action="restore", before=before, after=self._snapshot(memory), actor=actor)
        self.refresh_fts(db)
        return memory

    def history(self, db: Session, *, memory_id: uuid.UUID | None = None, limit: int = 100) -> list[MemoryHistory]:
        stmt = select(MemoryHistory).order_by(desc(MemoryHistory.created_at)).limit(max(1, min(limit, 500)))
        if memory_id is not None:
            stmt = stmt.where(MemoryHistory.memory_id == memory_id)
        return list(db.scalars(stmt).all())

    def governance_status(self, db: Session, *, budget_chars: int = MEMORY_DEFAULT_BUDGET_CHARS) -> dict[str, Any]:
        now = datetime.now(UTC)
        active = [
            item
            for item in db.scalars(select(Memory).where(Memory.archived.is_(False))).all()
            if not self._is_inactive(item, now=now)
        ]
        total_chars = sum(len(item.content or "") for item in active)
        expired = list(
            db.scalars(
                select(Memory)
                .where(Memory.archived.is_(False), Memory.valid_to.is_not(None), Memory.valid_to < now)
                .order_by(desc(Memory.valid_to))
                .limit(100)
            ).all()
        )
        missing_access = [item for item in active if not isinstance(item.metadata_json, dict) or not item.metadata_json.get("access")]
        return {
            "budget": {
                "limit_chars": budget_chars,
                "used_chars": total_chars,
                "remaining_chars": max(0, budget_chars - total_chars),
                "over_budget": total_chars > budget_chars,
            },
            "active_memories": len(active),
            "expired_memories": len(expired),
            "missing_access_policy": len(missing_access),
            "fts": self.fts_status(db),
            "embedding": self.vector.status(db) if self.vector is not None else {"status": "disabled", "provider": None},
        }

    def refresh_fts(self, db: Session) -> dict[str, Any]:
        backend = self._db_backend(db)
        if backend == "sqlite":
            return self._refresh_sqlite_fts(db)
        if backend == "postgresql":
            return self._postgres_fts_status(db)
        return {"backend": backend, "available": False, "indexed_memories": 0, "fallback": True}

    def fts_status(self, db: Session) -> dict[str, Any]:
        backend = self._db_backend(db)
        if backend == "sqlite":
            try:
                exists = int(
                    self._result_scalar(
                        db.execute(
                            text(
                                "SELECT count(*) FROM sqlite_master WHERE type IN ('table', 'virtual table') AND name = 'memories_fts'"
                            )
                        ),
                        0,
                    )
                    or 0
                ) > 0
                indexed = 0
                if exists:
                    indexed = int(self._result_scalar(db.execute(text("SELECT count(*) FROM memories_fts")), 0) or 0)
                return {"backend": "sqlite", "available": exists, "indexed_memories": indexed, "fallback": not exists}
            except Exception as exc:  # noqa: BLE001
                return {"backend": "sqlite", "available": False, "indexed_memories": 0, "fallback": True, "error": str(exc)}
        if backend == "postgresql":
            return self._postgres_fts_status(db)
        return {"backend": backend, "available": False, "indexed_memories": 0, "fallback": True}

    def apply_proposal(self, db: Session, proposal_id: uuid.UUID, *, actor: str = "admin") -> EvolutionProposal:
        proposal = db.get(EvolutionProposal, proposal_id)
        if proposal is None:
            raise KeyError(f"proposal not found: {proposal_id}")
        if proposal.status not in {"pending", "approved"}:
            raise ValueError(f"proposal is not applyable: {proposal.status}")
        if proposal.target_type != "memory":
            raise ValueError("only memory proposals are applyable here")
        action = proposal.action
        payload = proposal.payload or {}
        before: dict[str, Any] = {}
        result: dict[str, Any]
        if action == "create":
            memory = self._create_applied_memory(
                db,
                kind=str(payload.get("kind") or "agent_note"),
                content=str(payload["content"]),
                source=str(payload.get("source") or "proposal"),
                pinned=bool(payload.get("pinned", False)),
                importance=float(payload.get("importance", 0.5)),
                confidence=float(payload.get("confidence", 0.5)),
                stability=float(payload.get("stability", 0.5)),
                supersedes_id=uuid.UUID(str(payload["supersedes_id"])) if payload.get("supersedes_id") else None,
                source_turn_id=str(payload.get("source_turn_id") or ""),
                metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
            )
            result = {"ok": True, "memory_id": str(memory.id), "action": action}
        elif action == "supersede":
            memory_id = uuid.UUID(str(payload["memory_id"]))
            previous = db.get(Memory, memory_id)
            if previous is None:
                raise KeyError(f"memory not found: {memory_id}")
            before = {"memory": self._snapshot(previous)}
            replacement = {
                "kind": payload.get("kind") or previous.kind,
                "content": str(payload["content"]),
                "source": payload.get("source", "proposal"),
                "pinned": payload.get("pinned", previous.pinned),
                "importance": payload.get("importance", previous.importance),
                "confidence": payload.get("confidence", previous.confidence),
                "stability": payload.get("stability", previous.stability),
                "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
            }
            memory = self.supersede(db, memory_id, replacement)
            result = {"ok": True, "memory_id": str(memory.id), "supersedes_id": str(memory_id), "action": action}
        elif action == "archive":
            memory_id = uuid.UUID(str(payload["memory_id"]))
            previous = db.get(Memory, memory_id)
            if previous is None:
                raise KeyError(f"memory not found: {memory_id}")
            before = {"memory": self._snapshot(previous)}
            self.archive(db, memory_id)
            result = {"ok": True, "memory_id": str(memory_id), "action": action}
        elif action == "verify":
            memory_id = uuid.UUID(str(payload["memory_id"]))
            previous = db.get(Memory, memory_id)
            if previous is None:
                raise KeyError(f"memory not found: {memory_id}")
            before = {"memory": self._snapshot(previous)}
            memory = self.mark_verified(db, memory_id, confidence_delta=float(payload.get("confidence_delta", 0.05)))
            result = {"ok": True, "memory_id": str(memory.id), "action": action}
        elif action == "restore":
            history_id = uuid.UUID(str(payload["history_id"]))
            history = db.get(MemoryHistory, history_id)
            if history is None:
                raise KeyError(f"memory history not found: {history_id}")
            before = {"history": str(history_id)}
            memory = self.restore(db, history_id, actor=actor)
            result = {"ok": True, "memory_id": str(memory.id), "history_id": str(history_id), "action": action}
        else:
            raise ValueError("memory proposal action must be create, supersede, archive, verify, or restore")
        proposal.status = "applied"
        proposal.before_snapshot = before
        proposal.after_snapshot = result
        proposal.result = result | {"actor": actor}
        proposal.applied_at = datetime.now(UTC)
        if self.events:
            self.events.emit("memory.proposal.applied", {"proposal_id": str(proposal.id), "action": action})
            self.events.audit(
                "memory.proposal.apply",
                "evolution_proposal",
                target_id=str(proposal.id),
                payload={"action": action, "actor": actor},
            )
        return proposal

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    def _structured_candidates(self, db: Session, *, query: str, limit: int) -> list[Memory]:
        now = datetime.now(UTC)
        base = [
            Memory.archived.is_(False),
            Memory.status.not_in(sorted(MEMORY_EXPIRED_STATUSES)),
            or_(Memory.valid_to.is_(None), Memory.valid_to >= now),
        ]
        if query:
            tokens = self._tokens(query)
            clauses = [Memory.content.ilike(f"%{query}%"), Memory.kind.ilike(f"%{query}%"), Memory.source.ilike(f"%{query}%")]
            for token in sorted(tokens)[:6]:
                clauses.append(Memory.content.ilike(f"%{token}%"))
            stmt = (
                select(Memory)
                .where(*base, or_(*clauses))
                .order_by(desc(Memory.pinned), desc(Memory.importance), desc(Memory.updated_at))
                .limit(limit)
            )
        else:
            stmt = (
                select(Memory)
                .where(*base)
                .order_by(desc(Memory.pinned), desc(Memory.importance), desc(Memory.updated_at))
                .limit(limit)
            )
        return list(db.scalars(stmt).all())

    def _fts_search(self, db: Session, query: str, *, limit: int) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        backend = self._db_backend(db)
        if backend == "sqlite":
            return self._sqlite_fts_search(db, query, limit=limit)
        if backend == "postgresql":
            return self._postgres_fts_search(db, query, limit=limit)
        return []

    def _refresh_sqlite_fts(self, db: Session) -> dict[str, Any]:
        try:
            db.execute(
                text(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                        memory_id UNINDEXED,
                        kind,
                        content,
                        source,
                        metadata,
                        tokenize='unicode61'
                    )
                    """
                )
            )
            db.execute(text("DELETE FROM memories_fts"))
            memories = list(
                db.scalars(
                    select(Memory)
                    .where(Memory.archived.is_(False), Memory.status.not_in(sorted(MEMORY_EXPIRED_STATUSES)))
                    .order_by(desc(Memory.updated_at))
                    .limit(5000)
                ).all()
            )
            now = datetime.now(UTC)
            indexed = 0
            for memory in memories:
                if self._is_inactive(memory, now=now):
                    continue
                db.execute(
                    text(
                        """
                        INSERT INTO memories_fts(memory_id, kind, content, source, metadata)
                        VALUES (:memory_id, :kind, :content, :source, :metadata)
                        """
                    ),
                    {
                        "memory_id": str(memory.id),
                        "kind": memory.kind or "",
                        "content": memory.content or "",
                        "source": memory.source or "",
                        "metadata": str(memory.metadata_json or {}),
                    },
                )
                indexed += 1
            return {"backend": "sqlite", "available": True, "indexed_memories": indexed, "fallback": False}
        except Exception as exc:  # noqa: BLE001
            if self.events:
                self.events.emit("memory.fts.refresh_failed", {"backend": "sqlite", "error": str(exc)}, severity="warning")
            return {"backend": "sqlite", "available": False, "indexed_memories": 0, "fallback": True, "error": str(exc)}

    def _sqlite_fts_search(self, db: Session, query: str, *, limit: int) -> list[dict[str, Any]]:
        fts_query = self._safe_fts_query(query)
        if not fts_query:
            return []
        try:
            if not self.fts_status(db).get("available"):
                self.refresh_fts(db)
            rows = db.execute(
                text(
                    """
                    SELECT
                        memory_id,
                        bm25(memories_fts, 5.0, 2.0, 1.0, 0.3) AS raw_rank,
                        snippet(memories_fts, 2, '[', ']', ' ... ', 24) AS snippet
                    FROM memories_fts
                    WHERE memories_fts MATCH :query
                    ORDER BY raw_rank
                    LIMIT :limit
                    """
                ),
                {"query": fts_query, "limit": limit},
            ).mappings().all()
            return [
                {
                    "memory_id": str(row["memory_id"]),
                    "rank": 1.0 / (1.0 + abs(float(row["raw_rank"] or 0.0))),
                    "snippet": str(row["snippet"] or ""),
                }
                for row in rows
            ]
        except Exception:  # noqa: BLE001
            return []

    def _postgres_fts_search(self, db: Session, query: str, *, limit: int) -> list[dict[str, Any]]:
        tokens = sorted(self._tokens(query))
        if not tokens:
            return []
        tsquery = " | ".join(token.replace("'", " ") for token in tokens[:8])
        try:
            rows = db.execute(
                text(
                    """
                    WITH docs AS (
                        SELECT
                            id::text AS memory_id,
                            content,
                            setweight(to_tsvector('simple', coalesce(kind, '')), 'A') ||
                            setweight(to_tsvector('simple', coalesce(content, '')), 'B') ||
                            setweight(to_tsvector('simple', coalesce(source, '')), 'C') AS document
                        FROM memories
                        WHERE archived = false
                          AND status NOT IN ('archived', 'superseded')
                          AND (valid_to IS NULL OR valid_to >= now())
                    )
                    SELECT
                        memory_id,
                        ts_rank_cd(document, to_tsquery('simple', :query)) AS rank,
                        ts_headline('simple', content, to_tsquery('simple', :query), 'MaxWords=24, MinWords=8') AS snippet
                    FROM docs
                    WHERE document @@ to_tsquery('simple', :query)
                    ORDER BY rank DESC
                    LIMIT :limit
                    """
                ),
                {"query": tsquery, "limit": limit},
            ).mappings().all()
            return [{"memory_id": str(row["memory_id"]), "rank": float(row["rank"] or 0.0), "snippet": str(row["snippet"] or "")} for row in rows]
        except Exception:  # noqa: BLE001
            return []

    def _score_memory(self, memory: Memory, *, query: str, fts_hit: dict[str, Any], now: datetime) -> tuple[float, list[str]]:
        score = 0.0
        reasons: list[str] = []
        if fts_hit:
            score += min(3.0, float(fts_hit.get("rank") or 0.0) * 3.0)
            reasons.append("bm25")
        tokens = self._tokens(query)
        content_tokens = self._tokens(memory.content or "")
        if query and query.lower() in (memory.content or "").lower():
            score += 1.0
            reasons.append("exact_content")
        overlap = self._jaccard(tokens, content_tokens) if tokens else 0.0
        if overlap:
            score += min(1.0, overlap * 2.0)
            reasons.append("token_overlap")
        if memory.pinned:
            score += 0.8
            reasons.append("pinned")
        if memory.kind in L1_KINDS:
            score += 0.45
            reasons.append("l1")
        quality = (float(memory.importance or 0.0) * 0.45) + (float(memory.confidence or 0.0) * 0.25) + (float(memory.stability or 0.0) * 0.2)
        score += quality
        reasons.append("quality")
        score += self._recency_score(memory, now=now)
        return score, reasons

    @staticmethod
    def _recency_score(memory: Memory, *, now: datetime) -> float:
        updated = getattr(memory, "updated_at", None) or getattr(memory, "created_at", None)
        if updated is None:
            return 0.0
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        age_days = max(0.0, (now - updated).total_seconds() / 86400.0)
        return max(0.0, 0.45 * (1.0 - min(age_days, 90.0) / 90.0))

    def _mmr_dedup(self, ranked: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        selected_tokens: list[set[str]] = []
        for item in ranked:
            tokens = self._tokens(item["memory"].content or "")
            similarity = max((self._jaccard(tokens, existing) for existing in selected_tokens), default=0.0)
            if similarity >= 0.82:
                continue
            item["score"] = round(float(item["score"]) - similarity * 0.35, 4)
            if similarity:
                item.setdefault("match_reasons", []).append("dedup_penalty")
            selected.append(item)
            selected_tokens.append(tokens)
            if len(selected) >= limit:
                break
        selected.sort(key=lambda item: item["score"], reverse=True)
        return selected[:limit]

    @staticmethod
    def _safe_fts_query(value: str) -> str:
        tokens = MemoryService._tokens(value)
        if not tokens:
            cleaned = re.sub(r'"', " ", value).strip()
            return f'"{cleaned}"' if cleaned else ""
        return " OR ".join(f'"{token.replace(chr(34), chr(32))}"' for token in sorted(tokens)[:8])

    @staticmethod
    def _snippet(value: str, query: str, *, limit: int = 240) -> str:
        compact = re.sub(r"\s+", " ", value or "").strip()
        if not compact:
            return ""
        if not query:
            return compact[:limit]
        lowered = compact.lower()
        start = 0
        for token in [query.lower(), *sorted(MemoryService._tokens(query))]:
            index = lowered.find(token)
            if index >= 0:
                start = max(0, index - 60)
                break
        return ("..." if start else "") + compact[start : start + limit]

    @staticmethod
    def _db_backend(db: Session) -> str:
        try:
            return str(db.get_bind().dialect.name)
        except Exception:  # noqa: BLE001
            return "unknown"

    @staticmethod
    def _postgres_fts_status(db: Session) -> dict[str, Any]:
        try:
            exists = bool(MemoryService._result_scalar(db.execute(text("SELECT to_regclass('ix_memories_fts')")), None))
            indexed = int(MemoryService._result_scalar(db.execute(text("SELECT count(*) FROM memories WHERE archived = false")), 0) or 0)
            return {"backend": "postgresql", "available": exists, "indexed_memories": indexed, "fallback": not exists}
        except Exception as exc:  # noqa: BLE001
            return {"backend": "postgresql", "available": False, "indexed_memories": 0, "fallback": True, "error": str(exc)}

    @staticmethod
    def _result_scalar(result: Any, default: Any = None) -> Any:
        for method in ("scalar_one_or_none", "scalar"):
            if hasattr(result, method):
                try:
                    value = getattr(result, method)()
                    return default if value is None else value
                except Exception:  # noqa: BLE001
                    continue
        if hasattr(result, "first"):
            try:
                row = result.first()
            except Exception:  # noqa: BLE001
                return default
            if row is None:
                return default
            if isinstance(row, tuple):
                return row[0]
            return row
        return default

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None

    @staticmethod
    def _memory_access(metadata: dict[str, Any]) -> dict[str, Any]:
        access = metadata.get("access") if isinstance(metadata, dict) else None
        if isinstance(access, dict):
            return access
        return {"scope": str(metadata.get("scope") or "local"), "permission": str(metadata.get("permission") or "private")}

    @staticmethod
    def _is_inactive(memory: Memory, *, now: datetime) -> bool:
        if memory.archived or getattr(memory, "status", "active") in MEMORY_EXPIRED_STATUSES:
            return True
        valid_to = getattr(memory, "valid_to", None)
        if valid_to is None:
            return False
        if valid_to.tzinfo is None:
            valid_to = valid_to.replace(tzinfo=UTC)
        return valid_to < now

    def _record_history(
        self,
        db: Session,
        memory: Memory | None,
        *,
        action: str,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> None:
        db.add(
            MemoryHistory(
                memory_id=getattr(memory, "id", None),
                action=action,
                before_snapshot={"memory": before} if before else {},
                after_snapshot={"memory": after} if after else {},
                actor=actor or "system",
            )
        )

    @staticmethod
    def _apply_snapshot(memory: Memory, snapshot: dict[str, Any]) -> None:
        memory.kind = str(snapshot.get("kind") or memory.kind or "agent_note")
        memory.content = str(snapshot.get("content") or memory.content or "")
        memory.source = str(snapshot.get("source") or memory.source or "restore")
        memory.status = str(snapshot.get("status") or "active")
        memory.pinned = bool(snapshot.get("pinned", False))
        memory.archived = bool(snapshot.get("archived", False))
        memory.importance = MemoryService._clamp(float(snapshot.get("importance", 0.5)))
        memory.confidence = MemoryService._clamp(float(snapshot.get("confidence", 0.5)))
        memory.stability = MemoryService._clamp(float(snapshot.get("stability", 0.5)))
        memory.provenance = snapshot.get("provenance") if isinstance(snapshot.get("provenance"), dict) else {}
        memory.metadata_json = snapshot.get("metadata") if isinstance(snapshot.get("metadata"), dict) else {}

    def _detect_conflicts(self, db: Session, memory: Memory) -> None:
        tokens = self._tokens(memory.content)
        if len(tokens) < 4:
            return
        candidates = list(
            db.scalars(
                select(Memory)
                .where(Memory.id != memory.id, Memory.archived.is_(False), Memory.kind == memory.kind)
                .order_by(desc(Memory.updated_at))
                .limit(50)
            ).all()
        )
        for candidate in candidates:
            overlap = self._jaccard(tokens, self._tokens(candidate.content))
            if overlap < 0.62 or candidate.content.strip() == memory.content.strip():
                continue
            existing = db.scalar(
                select(MemoryConflict)
                .where(
                    or_(
                        (MemoryConflict.left_memory_id == candidate.id)
                        & (MemoryConflict.right_memory_id == memory.id),
                        (MemoryConflict.left_memory_id == memory.id)
                        & (MemoryConflict.right_memory_id == candidate.id),
                    ),
                    MemoryConflict.status == "open",
                )
                .limit(1)
            )
            if existing is None:
                db.add(
                    MemoryConflict(
                        left_memory_id=candidate.id,
                        right_memory_id=memory.id,
                        status="open",
                        reason=f"Similar {memory.kind} memories may conflict (token overlap {overlap:.2f}).",
                    )
                )

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {item for item in re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]+", text.lower()) if len(item) > 1}

    @staticmethod
    def _jaccard(left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        return len(left & right) / len(left | right)

    @staticmethod
    def _snapshot(memory: Memory) -> dict[str, Any]:
        return {
            "id": str(memory.id),
            "kind": memory.kind,
            "content": memory.content,
            "source": memory.source,
            "status": getattr(memory, "status", "active"),
            "pinned": memory.pinned,
            "archived": memory.archived,
            "importance": memory.importance,
            "confidence": memory.confidence,
            "stability": memory.stability,
            "provenance": getattr(memory, "provenance", {}) or {},
            "metadata": memory.metadata_json or {},
        }
