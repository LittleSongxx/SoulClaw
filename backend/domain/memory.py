"""Layered long-term memory repository and retriever."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session

from backend.domain.workspace import WorkspaceService
from backend.infra.config import Settings, get_settings
from backend.infra.events import RuntimeEventBus
from backend.infra.models import EvolutionProposal, Memory, MemoryConflict, MemoryProbe

L1_KINDS = {"control_axiom", "pinned_fact"}
L2_KINDS = {"episodic", "user_fact", "agent_note", "error_signal"}
L3_KINDS = {"semantic", "skill_trace", "project_knowledge"}
MEMORY_MARKER_RE = re.compile(r"<!--\s*soulclaw:memory\s+id=([0-9a-fA-F-]+)\s*-->")
MEMORY_LINE_RE = re.compile(r"^-\s+\((?P<meta>[^)]*)\)\s+(?P<content>.*)$", re.DOTALL)


class MemoryService:
    def __init__(
        self,
        settings: Settings | None = None,
        events: RuntimeEventBus | None = None,
        workspace: WorkspaceService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.events = events
        self.workspace = workspace or WorkspaceService(settings=self.settings, events=events)

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
            status="active",
            pinned=pinned,
            importance=self._clamp(importance),
            confidence=self._clamp(confidence),
            stability=self._clamp(stability),
            supersedes_id=supersedes_id,
            source_turn_id=source_turn_id,
            provenance={
                "source": source,
                **(metadata.get("provenance", {}) if isinstance(metadata, dict) and isinstance(metadata.get("provenance"), dict) else {}),
            },
            metadata_json=metadata or {},
        )
        db.add(memory)
        if supersedes_id is not None:
            previous = db.get(Memory, supersedes_id)
            if previous is not None:
                previous.archived = True
                previous.status = "superseded"
        db.flush()
        memory.source_file_marker = self._marker(memory)
        self._detect_conflicts(db, memory)
        self._append_to_memory_file(memory)
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
            records[str(memory.id)] = {"score": 1.0, "source": "memory_index", "memory": memory}
        return list(records.values())[:limit]

    def get(self, db: Session, memory_id: uuid.UUID) -> Memory | None:
        return db.get(Memory, memory_id)

    def file_context(self, *, limit_chars: int = 6000) -> str:
        return self.workspace.read("memory").content[: max(1, limit_chars)]

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
        previous.status = "superseded"
        memory = self.create(
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
        self._rewrite_memory_file_entry(previous)
        self._rewrite_memory_file_entry(memory)
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
        memory.last_verified_at = datetime.now(UTC)
        memory.confidence = self._clamp(memory.confidence + confidence_delta)
        memory.stability = self._clamp(memory.stability + confidence_delta / 2)
        self._rewrite_memory_file_entry(memory)
        if self.events:
            self.events.audit("memory.verify", "memory", target_id=str(memory_id), payload={"confidence_delta": confidence_delta})
        return memory

    def archive(self, db: Session, memory_id: uuid.UUID) -> Memory:
        memory = db.get(Memory, memory_id)
        if memory is None:
            raise KeyError(f"memory not found: {memory_id}")
        memory.archived = True
        memory.status = "archived"
        self._rewrite_memory_file_entry(memory)
        if self.events:
            self.events.emit("memory.archive", {"memory_id": str(memory_id)})
            self.events.audit("memory.archive", "memory", target_id=str(memory_id))
        return memory

    def sync_from_memory_file(self, db: Session) -> dict[str, Any]:
        item = self.workspace.read("memory")
        lines = item.content.splitlines()
        updated = 0
        missing = 0
        for index, line in enumerate(lines):
            marker = MEMORY_MARKER_RE.match(line.strip())
            if marker is None:
                continue
            try:
                memory_id = uuid.UUID(marker.group(1))
            except ValueError:
                continue
            memory = db.get(Memory, memory_id)
            if memory is None:
                missing += 1
                continue
            if index + 1 >= len(lines):
                continue
            parsed = self._parse_memory_line(lines[index + 1])
            if parsed is None:
                continue
            metadata, content = parsed
            memory.kind = metadata.get("kind") or memory.kind
            memory.content = content or memory.content
            memory.source = metadata.get("source") or memory.source
            memory.status = metadata.get("status") or memory.status or "active"
            memory.archived = memory.status in {"archived", "superseded"}
            for key in ("importance", "confidence", "stability"):
                if key in metadata:
                    try:
                        setattr(memory, key, self._clamp(float(metadata[key])))
                    except ValueError:
                        continue
            memory.source_file_marker = self._marker(memory)
            memory.provenance = {
                **(memory.provenance or {}),
                "synced_from": item.path,
                "synced_at": datetime.now(UTC).isoformat(),
            }
            updated += 1
        if self.events:
            self.events.emit("memory.file_sync", {"updated": updated, "missing": missing, "path": item.path})
            self.events.audit(
                "memory.file_sync",
                "workspace_file",
                target_id="memory",
                payload={"updated": updated, "missing": missing, "path": item.path},
            )
        return {"updated": updated, "missing": missing, "path": item.path}

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
            memory = self.create(
                db,
                kind=str(payload.get("kind") or "agent_note"),
                content=str(payload["content"]),
                source=str(payload.get("source") or "proposal"),
                pinned=bool(payload.get("pinned", False)),
                importance=float(payload.get("importance", 0.5)),
                confidence=float(payload.get("confidence", 0.5)),
                stability=float(payload.get("stability", 0.5)),
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
        else:
            raise ValueError("memory proposal action must be create, supersede, archive, or verify")
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

    def _append_to_memory_file(self, memory: Memory) -> None:
        try:
            item = self.workspace.read("memory")
            marker = self._marker(memory)
            if marker in item.content:
                self._rewrite_memory_file_entry(memory)
                return
            line = f"\n\n{marker}\n{self._memory_file_line(memory)}\n"
            self.workspace.write("memory", item.content.rstrip() + line, actor="memory-service")
        except Exception as exc:  # noqa: BLE001
            if self.events:
                self.events.emit(
                    "memory.file_append.failed",
                    {"memory_id": str(memory.id), "error": str(exc)},
                    severity="warning",
                )

    def _rewrite_memory_file_entry(self, memory: Memory) -> None:
        try:
            item = self.workspace.read("memory")
            marker = self._marker(memory)
            lines = item.content.splitlines()
            for index, line in enumerate(lines):
                if line.strip() != marker:
                    continue
                if index + 1 < len(lines):
                    lines[index + 1] = self._memory_file_line(memory)
                else:
                    lines.append(self._memory_file_line(memory))
                self.workspace.write("memory", "\n".join(lines).rstrip() + "\n", actor="memory-service")
                return
        except Exception as exc:  # noqa: BLE001
            if self.events:
                self.events.emit(
                    "memory.file_rewrite.failed",
                    {"memory_id": str(memory.id), "error": str(exc)},
                    severity="warning",
                )

    @staticmethod
    def _marker(memory: Memory) -> str:
        return f"<!-- soulclaw:memory id={memory.id} -->"

    @staticmethod
    def _memory_file_line(memory: Memory) -> str:
        status = getattr(memory, "status", "") or ("archived" if memory.archived else "active")
        return (
            f"- ({memory.kind}, source={memory.source}, status={status}, "
            f"importance={memory.importance:.2f}, confidence={memory.confidence:.2f}, "
            f"stability={memory.stability:.2f}) {memory.content.strip()}"
        )

    @staticmethod
    def _parse_memory_line(line: str) -> tuple[dict[str, str], str] | None:
        match = MEMORY_LINE_RE.match(line.strip())
        if match is None:
            return None
        metadata: dict[str, str] = {}
        for part in match.group("meta").split(","):
            part = part.strip()
            if not part:
                continue
            if "=" in part:
                key, value = part.split("=", 1)
                metadata[key.strip()] = value.strip()
            elif "kind" not in metadata:
                metadata["kind"] = part
        return metadata, match.group("content").strip()

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
            "source_file_marker": getattr(memory, "source_file_marker", "") or "",
            "provenance": getattr(memory, "provenance", {}) or {},
            "metadata": memory.metadata_json or {},
        }
