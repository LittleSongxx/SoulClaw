"""Structured authority for long-lived core context blocks."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.domain.workspace import WorkspaceService
from backend.infra.events import RuntimeEventBus
from backend.infra.models import CoreContextBlock, EvolutionProposal

CORE_BLOCK_KEYS = ("soul", "user", "heartbeat")

DEFAULT_CORE_BLOCKS: dict[str, dict[str, Any]] = {
    "soul": {
        "title": "SoulClaw Operating Identity",
        "content": (
            "SoulClaw is a local-first long-term personal assistant. It should be careful, useful, "
            "transparent about uncertainty, and respectful of the user's preferences. Treat structured "
            "core context and approved memories as durable state; treat projections as readable exports."
        ),
        "confidence": 0.9,
        "metadata": {"projection_path": "SOUL.md", "prompt_role": "soul"},
    },
    "user": {
        "title": "User Profile",
        "content": (
            "No durable user profile facts have been approved yet. Store stable preferences, profile facts, "
            "relationship context, and long-running constraints here only through reviewed proposals."
        ),
        "confidence": 0.5,
        "metadata": {"projection_path": "USER.md", "prompt_role": "human"},
    },
    "heartbeat": {
        "title": "Heartbeat Tasks",
        "content": "No active proactive tasks are configured.",
        "confidence": 0.7,
        "metadata": {"projection_path": "HEARTBEAT.md", "tasks": []},
    },
}


@dataclass(frozen=True)
class CoreProjection:
    kind: str
    path: str
    content: str


class CoreContextService:
    def __init__(self, *, workspace: WorkspaceService | None = None, events: RuntimeEventBus | None = None) -> None:
        self.workspace = workspace
        self.events = events

    def ensure_defaults(self, db: Session) -> list[CoreContextBlock]:
        blocks: list[CoreContextBlock] = []
        for key in CORE_BLOCK_KEYS:
            existing = self.get(db, key)
            if existing is not None:
                blocks.append(existing)
                continue
            defaults = DEFAULT_CORE_BLOCKS[key]
            block = CoreContextBlock(
                block_key=key,
                title=str(defaults["title"]),
                content=str(defaults["content"]),
                status="active",
                version=1,
                confidence=float(defaults["confidence"]),
                source="system",
                metadata_json=dict(defaults["metadata"]),
            )
            db.add(block)
            blocks.append(block)
        db.flush()
        return blocks

    def list(self, db: Session, *, include_inactive: bool = False) -> list[CoreContextBlock]:
        self.ensure_defaults(db)
        stmt = select(CoreContextBlock).order_by(CoreContextBlock.block_key)
        if not include_inactive:
            stmt = stmt.where(CoreContextBlock.status == "active")
        return list(db.scalars(stmt).all())

    def get(self, db: Session, block_key: str) -> CoreContextBlock | None:
        key = self._normalize_key(block_key)
        return db.scalar(select(CoreContextBlock).where(CoreContextBlock.block_key == key))

    def prompt_context(self, db: Session) -> dict[str, CoreContextBlock | None]:
        self.ensure_defaults(db)
        return {key: self.get(db, key) for key in ("soul", "user")}

    def heartbeat_tasks(self, db: Session) -> list[str]:
        self.ensure_defaults(db)
        block = self.get(db, "heartbeat")
        if block is None:
            return []
        metadata = block.metadata_json or {}
        tasks = metadata.get("tasks")
        if isinstance(tasks, list):
            return [str(item).strip() for item in tasks if str(item).strip()]
        return [
            line.removeprefix("-").strip()
            for line in (block.content or "").splitlines()
            if line.strip().startswith("-") and line.removeprefix("-").strip()
        ]

    def apply_proposal(self, db: Session, proposal_id: uuid.UUID, *, actor: str = "admin") -> EvolutionProposal:
        proposal = db.get(EvolutionProposal, proposal_id)
        if proposal is None:
            raise KeyError(f"proposal not found: {proposal_id}")
        if proposal.status not in {"pending", "approved"}:
            raise ValueError(f"proposal is not applyable: {proposal.status}")
        if proposal.target_type != "core_context":
            raise ValueError("only core context proposals are applyable here")
        payload = proposal.payload or {}
        key = self._normalize_key(str(payload.get("block_key") or ""))
        if key not in CORE_BLOCK_KEYS:
            raise ValueError(f"unsupported core context block: {key}")
        block = self.get(db, key)
        if block is None:
            self.ensure_defaults(db)
            block = self.get(db, key)
        if block is None:
            raise KeyError(f"core context block not found: {key}")
        before = self._snapshot(block)
        block.title = str(payload.get("title") or block.title or DEFAULT_CORE_BLOCKS[key]["title"])
        block.content = str(payload.get("content") or "")
        block.status = str(payload.get("status") or "active")
        block.version = int(block.version or 1) + 1
        block.confidence = self._clamp(float(payload.get("confidence", block.confidence or 0.5)))
        block.source = str(payload.get("source") or "proposal")
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        block.metadata_json = {**(block.metadata_json or {}), **metadata, "last_actor": actor, "updated_from_proposal": str(proposal.id)}
        proposal.status = "applied"
        proposal.before_snapshot = {"block": before}
        proposal.after_snapshot = {"block": self._snapshot(block)}
        proposal.result = {"ok": True, "block_key": key, "actor": actor}
        proposal.applied_at = datetime.now(UTC)
        db.flush()
        self.refresh_projections(db)
        if self.events:
            self.events.emit("core_context.proposal.applied", {"proposal_id": str(proposal.id), "block_key": key})
            self.events.audit("core_context.update", "core_context_block", target_id=key, payload={"actor": actor, "proposal_id": str(proposal.id)})
        return proposal

    def projections(self, db: Session) -> list[CoreProjection]:
        self.ensure_defaults(db)
        blocks = {block.block_key: block for block in self.list(db)}
        memory_projection = self.memory_projection(db)
        return [
            CoreProjection("soul", "SOUL.md", self._block_projection(blocks["soul"])),
            CoreProjection("user", "USER.md", self._block_projection(blocks["user"])),
            CoreProjection("heartbeat", "HEARTBEAT.md", self._heartbeat_projection(blocks["heartbeat"])),
            CoreProjection("memory", "memory/MEMORY.md", memory_projection),
        ]

    def refresh_projections(self, db: Session) -> list[CoreProjection]:
        projections = self.projections(db)
        if self.workspace is None:
            return projections
        for projection in projections:
            self.workspace.write(projection.kind, projection.content, actor="core-context-projection")
        return projections

    def memory_projection(self, db: Session, *, limit: int = 200) -> str:
        from backend.infra.models import Memory

        memories = list(
            db.scalars(
                select(Memory)
                .where(Memory.archived.is_(False), Memory.status == "active")
                .order_by(Memory.kind, Memory.updated_at.desc())
                .limit(max(1, min(limit, 500)))
            ).all()
        )
        lines = [
            "# MEMORY",
            "",
            "> Generated projection. Postgres structured memories are authoritative; edit drafts through proposals.",
            "",
        ]
        if not memories:
            lines.append("No approved long-term memories yet.")
            return "\n".join(lines).rstrip() + "\n"
        for memory in memories:
            lines.append(
                f"- `{memory.kind}` [{memory.source}] confidence={memory.confidence:.2f} "
                f"stability={memory.stability:.2f}: {memory.content.strip()}"
            )
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _block_projection(block: CoreContextBlock) -> str:
        return (
            f"# {block.title or block.block_key.upper()}\n\n"
            "> Generated projection. Postgres core context is authoritative; propose edits through the console/API.\n\n"
            f"{(block.content or '').strip()}\n"
        )

    @staticmethod
    def _heartbeat_projection(block: CoreContextBlock) -> str:
        metadata = block.metadata_json or {}
        tasks = metadata.get("tasks") if isinstance(metadata.get("tasks"), list) else []
        lines = [
            "# HEARTBEAT",
            "",
            "> Generated projection. Structured heartbeat state is authoritative; propose edits through the console/API.",
            "",
            "## Active Tasks",
            "",
        ]
        if tasks:
            lines.extend(f"- {str(task).strip()}" for task in tasks if str(task).strip())
        else:
            lines.append("- No active proactive tasks.")
        if block.content.strip():
            lines.extend(["", "## Notes", "", block.content.strip()])
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _snapshot(block: CoreContextBlock | None) -> dict[str, Any]:
        if block is None:
            return {}
        return {
            "id": str(block.id),
            "block_key": block.block_key,
            "title": block.title,
            "content": block.content,
            "status": block.status,
            "version": block.version,
            "confidence": block.confidence,
            "source": block.source,
            "metadata": block.metadata_json or {},
        }

    @staticmethod
    def _normalize_key(block_key: str) -> str:
        return block_key.strip().lower()

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, float(value)))
