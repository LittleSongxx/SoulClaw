"""Unified evolution proposal application."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from backend.domain.core_context import CoreContextService
from backend.domain.memory import MemoryService
from backend.domain.skills import SkillService
from backend.domain.wiki import WikiService
from backend.infra.models import EvolutionProposal


class EvolutionService:
    def __init__(
        self,
        *,
        skills: SkillService,
        memory: MemoryService,
        wiki: WikiService | None = None,
        core_context: CoreContextService | None = None,
    ) -> None:
        self.skills = skills
        self.memory = memory
        self.wiki = wiki
        self.core_context = core_context

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
        if proposal.target_type == "core_context":
            if self.core_context is None:
                raise ValueError("core context evolution service is not configured")
            return self.core_context.apply_proposal(db, proposal_id, actor=actor)
        raise ValueError(f"unsupported proposal target_type: {proposal.target_type}")
