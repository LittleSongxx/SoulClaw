"""SQLAlchemy-backed review queue for self-evolving changes.

The proposal queue is deliberately small and generic. Memory writes,
skill edits, wiki facts, and compose recipes all land here first with
their evidence, risk, and payload. A separate apply step performs the
actual mutation and records before/after/result JSON for rollback and
audit surfaces.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import desc

from ..db.models import EvolutionProposal
from ..db.session import session_scope


STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_APPLIED = "applied"
STATUS_FAILED = "failed"
STATUS_ROLLED_BACK = "rolled_back"
VALID_STATUSES = frozenset({
    STATUS_PENDING,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_APPLIED,
    STATUS_FAILED,
    STATUS_ROLLED_BACK,
})

VALID_TARGET_TYPES = frozenset({"memory", "skill", "wiki", "workflow"})
VALID_RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})


class EvolutionProposalError(Exception):
    """Raised for caller-correctable proposal queue failures."""


def _json_dump(value: Any) -> str:
    try:
        return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return json.dumps({"repr": repr(value)}, ensure_ascii=False, sort_keys=True)


def _json_load(raw: str, default: Any) -> Any:
    try:
        parsed = json.loads(raw or "")
    except (TypeError, ValueError):
        return default
    return parsed if parsed is not None else default


def _clamp_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.5
    return max(0.0, min(1.0, confidence))


def _row_to_dict(row: EvolutionProposal) -> dict[str, Any]:
    return {
        "id": row.id,
        "target_type": row.target_type,
        "action": row.action,
        "payload": _json_load(row.payload_json, {}),
        "evidence": _json_load(row.evidence_json, {}),
        "before": _json_load(row.before_json, {}),
        "after": _json_load(row.after_json, {}),
        "result": _json_load(row.result_json, {}),
        "confidence": float(row.confidence or 0.0),
        "risk_level": row.risk_level,
        "status": row.status,
        "source": row.source,
        "error": row.error or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "applied_at": row.applied_at.isoformat() if row.applied_at else None,
        "rolled_back_at": row.rolled_back_at.isoformat() if row.rolled_back_at else None,
    }


class EvolutionProposalStore:
    """CRUD and state transitions for :class:`EvolutionProposal`."""

    def create(
        self,
        *,
        target_type: str,
        action: str,
        payload: dict[str, Any],
        evidence: Optional[dict[str, Any]] = None,
        confidence: float = 0.5,
        risk_level: str = "medium",
        source: str = "post_turn",
    ) -> dict[str, Any]:
        target_type = str(target_type or "").strip().lower()
        action = str(action or "").strip().lower()
        risk_level = str(risk_level or "medium").strip().lower()
        source = str(source or "post_turn").strip()[:64]
        if target_type not in VALID_TARGET_TYPES:
            raise EvolutionProposalError(
                f"invalid target_type {target_type!r}; expected {sorted(VALID_TARGET_TYPES)}"
            )
        if not action:
            raise EvolutionProposalError("action is required")
        if risk_level not in VALID_RISK_LEVELS:
            raise EvolutionProposalError(
                f"invalid risk_level {risk_level!r}; expected {sorted(VALID_RISK_LEVELS)}"
            )
        if not isinstance(payload, dict):
            raise EvolutionProposalError("payload must be a JSON object")
        if evidence is not None and not isinstance(evidence, dict):
            raise EvolutionProposalError("evidence must be a JSON object")
        with session_scope() as session:
            row = EvolutionProposal(
                target_type=target_type,
                action=action,
                payload_json=_json_dump(payload),
                evidence_json=_json_dump(evidence or {}),
                confidence=_clamp_confidence(confidence),
                risk_level=risk_level,
                status=STATUS_PENDING,
                source=source,
            )
            session.add(row)
            session.flush()
            session.refresh(row)
            return _row_to_dict(row)

    def get(self, proposal_id: int) -> Optional[dict[str, Any]]:
        with session_scope() as session:
            row = session.get(EvolutionProposal, int(proposal_id))
            return _row_to_dict(row) if row is not None else None

    def list(
        self,
        *,
        status: Optional[str] = None,
        target_type: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit or 100)))
        status = str(status).strip().lower() if status else None
        target_type = str(target_type).strip().lower() if target_type else None
        if status is not None and status not in VALID_STATUSES:
            raise EvolutionProposalError(
                f"invalid status {status!r}; expected {sorted(VALID_STATUSES)}"
            )
        if target_type is not None and target_type not in VALID_TARGET_TYPES:
            raise EvolutionProposalError(
                f"invalid target_type {target_type!r}; expected {sorted(VALID_TARGET_TYPES)}"
            )
        with session_scope() as session:
            q = session.query(EvolutionProposal)
            if status is not None:
                q = q.filter(EvolutionProposal.status == status)
            if target_type is not None:
                q = q.filter(EvolutionProposal.target_type == target_type)
            q = q.order_by(desc(EvolutionProposal.created_at), desc(EvolutionProposal.id)).limit(limit)
            return [_row_to_dict(row) for row in q.all()]

    def approve(self, proposal_id: int) -> dict[str, Any]:
        return self._transition(proposal_id, STATUS_APPROVED)

    def reject(self, proposal_id: int, *, reason: str = "") -> dict[str, Any]:
        return self._transition(
            proposal_id,
            STATUS_REJECTED,
            result={"reason": reason} if reason else {},
        )

    def mark_applied(
        self,
        proposal_id: int,
        *,
        before: Optional[dict[str, Any]] = None,
        after: Optional[dict[str, Any]] = None,
        result: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        now = datetime.utcnow()
        with session_scope() as session:
            row = session.get(EvolutionProposal, int(proposal_id))
            if row is None:
                raise EvolutionProposalError(f"proposal #{proposal_id} not found")
            row.status = STATUS_APPLIED
            row.before_json = _json_dump(before or {})
            row.after_json = _json_dump(after or {})
            row.result_json = _json_dump(result or {})
            row.error = ""
            row.applied_at = now
            row.reviewed_at = row.reviewed_at or now
            row.updated_at = now
            session.flush()
            session.refresh(row)
            return _row_to_dict(row)

    def mark_failed(self, proposal_id: int, *, error: str) -> dict[str, Any]:
        return self._transition(
            proposal_id,
            STATUS_FAILED,
            error=str(error or "apply failed"),
        )

    def mark_rolled_back(
        self,
        proposal_id: int,
        *,
        result: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        now = datetime.utcnow()
        with session_scope() as session:
            row = session.get(EvolutionProposal, int(proposal_id))
            if row is None:
                raise EvolutionProposalError(f"proposal #{proposal_id} not found")
            if row.status != STATUS_APPLIED:
                raise EvolutionProposalError(
                    f"proposal #{proposal_id} status is {row.status!r}; only applied proposals can roll back"
                )
            row.status = STATUS_ROLLED_BACK
            row.result_json = _json_dump(result or {})
            row.rolled_back_at = now
            row.updated_at = now
            session.flush()
            session.refresh(row)
            return _row_to_dict(row)

    def stats(self) -> dict[str, Any]:
        with session_scope() as session:
            rows = session.query(EvolutionProposal.status, EvolutionProposal.target_type).all()
        by_status: dict[str, int] = {}
        by_target: dict[str, int] = {}
        for status, target in rows:
            by_status[str(status)] = by_status.get(str(status), 0) + 1
            by_target[str(target)] = by_target.get(str(target), 0) + 1
        return {
            "total": len(rows),
            "by_status": by_status,
            "by_target_type": by_target,
        }

    def _transition(
        self,
        proposal_id: int,
        status: str,
        *,
        result: Optional[dict[str, Any]] = None,
        error: str = "",
    ) -> dict[str, Any]:
        if status not in VALID_STATUSES:
            raise EvolutionProposalError(f"invalid status {status!r}")
        now = datetime.utcnow()
        with session_scope() as session:
            row = session.get(EvolutionProposal, int(proposal_id))
            if row is None:
                raise EvolutionProposalError(f"proposal #{proposal_id} not found")
            if row.status in {STATUS_APPLIED, STATUS_ROLLED_BACK} and status not in {STATUS_FAILED}:
                raise EvolutionProposalError(
                    f"proposal #{proposal_id} is already terminal ({row.status})"
                )
            row.status = status
            row.error = str(error or "")
            if result is not None:
                row.result_json = _json_dump(result)
            if status in {STATUS_APPROVED, STATUS_REJECTED}:
                row.reviewed_at = now
            row.updated_at = now
            session.flush()
            session.refresh(row)
            return _row_to_dict(row)
