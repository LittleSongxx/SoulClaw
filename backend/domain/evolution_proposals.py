"""Global evolution proposal repository.

This service owns proposal creation, listing, and rejection. Domain services
still own applying their own proposal types.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from backend.domain.skills import SkillService, normalize_skill_key, snapshot_checksum
from backend.infra.events import RuntimeEventBus
from backend.infra.models import EvolutionProposal

PROPOSAL_TARGET_TYPES = {"skill", "memory", "wiki", "core_context"}


class EvolutionProposalService:
    def __init__(self, *, skills: SkillService | None = None, events: RuntimeEventBus | None = None) -> None:
        self.skills = skills
        self.events = events

    def create(
        self,
        db: Session,
        *,
        target_type: str,
        action: str,
        payload: dict[str, Any],
        evidence: dict[str, Any] | None = None,
        risk_level: str = "medium",
    ) -> EvolutionProposal:
        target = self._normalize_target(target_type)
        payload = dict(payload)
        before_snapshot: dict[str, Any] = {}
        target_checksum = ""

        if target == "skill":
            before_snapshot, target_checksum = self._skill_before_snapshot(payload)

        proposal = EvolutionProposal(
            target_type=target,
            action=action,
            status="pending",
            risk_level=risk_level,
            payload=payload,
            evidence=evidence or {},
            before_snapshot=before_snapshot,
            target_checksum=target_checksum,
        )
        db.add(proposal)
        db.flush()
        if self.events:
            self.events.emit(
                "evolution.proposal.created",
                {"proposal_id": str(proposal.id), "target_type": target, "action": action},
            )
            self.events.audit(
                "evolution.proposal.create",
                "evolution_proposal",
                target_id=str(proposal.id),
                payload={"target_type": target, "action": action},
            )
        return proposal

    def list(
        self,
        db: Session,
        *,
        status: str | None = None,
        target_type: str | None = None,
        limit: int = 100,
    ) -> list[EvolutionProposal]:
        stmt = select(EvolutionProposal).order_by(desc(EvolutionProposal.created_at)).limit(max(1, min(limit, 500)))
        if status:
            stmt = stmt.where(EvolutionProposal.status == status)
        if target_type:
            stmt = stmt.where(EvolutionProposal.target_type == self._normalize_target(target_type))
        return list(db.scalars(stmt).all())

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
            self.events.audit(
                "evolution.proposal.reject",
                "evolution_proposal",
                target_id=str(proposal.id),
                payload={"target_type": proposal.target_type, "actor": actor},
            )
        return proposal

    def _skill_before_snapshot(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        if self.skills is None:
            return {}, ""
        skill_key = normalize_skill_key(str(payload.get("skill_key") or ""))
        files = payload.get("files")
        if not skill_key or not isinstance(files, dict):
            return {}, ""
        current = self.skills.snapshot_for_proposal(skill_key)
        proposed = {str(path): str(content) for path, content in files.items()}
        target_checksum = snapshot_checksum(current)
        payload.setdefault("base_checksum", target_checksum)
        payload.setdefault("diff", self.skills.diff_summary_for_proposal(current, proposed))
        return {"files": current, "checksum": target_checksum}, target_checksum

    @staticmethod
    def _normalize_target(target_type: str) -> str:
        target = target_type.strip().lower()
        if target not in PROPOSAL_TARGET_TYPES:
            raise ValueError(f"unsupported proposal target_type: {target_type}")
        return target
