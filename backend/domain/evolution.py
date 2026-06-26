"""Unified evolution proposal application."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from backend.domain.memory import MemoryService
from backend.domain.skills import SkillService
from backend.domain.wiki import WikiService
from backend.domain.workspace import WorkspaceService
from backend.infra.events import RuntimeEventBus
from backend.infra.models import EvolutionProposal


class EvolutionService:
    def __init__(
        self,
        *,
        skills: SkillService,
        memory: MemoryService,
        wiki: WikiService | None = None,
        workspace: WorkspaceService | None = None,
        events: RuntimeEventBus | None = None,
    ) -> None:
        self.skills = skills
        self.memory = memory
        self.wiki = wiki
        self.workspace = workspace
        self.events = events

    def apply(self, db: Session, proposal_id: uuid.UUID, *, actor: str = "admin") -> EvolutionProposal:
        proposal = db.get(EvolutionProposal, proposal_id)
        if proposal is None:
            raise KeyError(f"proposal not found: {proposal_id}")
        if proposal.target_type == "skill":
            return self.skills.apply_proposal(db, proposal_id, actor=actor)
        if proposal.target_type == "memory":
            return self.memory.apply_proposal(db, proposal_id, actor=actor)
        if proposal.target_type == "wiki":
            if self.wiki is None:
                raise ValueError("wiki evolution service is not configured")
            return self.wiki.apply_proposal(db, proposal_id, actor=actor)
        if proposal.target_type in {"persona", "user", "heartbeat", "workspace_file"}:
            if self.workspace is None:
                raise ValueError("workspace evolution service is not configured")
            return self.workspace.apply_proposal(db, proposal_id, actor=actor)
        raise ValueError(f"unsupported proposal target_type: {proposal.target_type}")

    def reject(self, db: Session, proposal_id: uuid.UUID, *, actor: str = "admin", reason: str = "") -> EvolutionProposal:
        proposal = db.get(EvolutionProposal, proposal_id)
        if proposal is None:
            raise KeyError(f"proposal not found: {proposal_id}")
        if proposal.status not in {"pending", "approved"}:
            raise ValueError(f"proposal is not rejectable: {proposal.status}")
        proposal.status = "rejected"
        proposal.result = {"ok": False, "actor": actor, "reason": reason}
        proposal.applied_at = datetime.now(UTC)
        if self.events:
            self.events.emit(
                "evolution.proposal.rejected",
                {"proposal_id": str(proposal.id), "target_type": proposal.target_type},
            )
        return proposal
