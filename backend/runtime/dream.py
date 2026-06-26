"""Dream/self-evolution review jobs for conservative skill proposals."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from backend.domain.skills import SkillService
from backend.infra.events import RuntimeEventBus
from backend.infra.models import EvolutionProposal, Memory, ToolRun


@dataclass(frozen=True)
class DreamReviewResult:
    scanned_memories: int
    failed_tool_runs: int
    proposals_created: int
    proposal_ids: list[str]


class DreamRuntime:
    """Turns error signals and failed tool runs into reviewable proposals.

    It never writes skill files. The output is a pending evolution proposal
    with evidence, required checks, and a draft SKILL.md that must still pass
    lint/test before apply.
    """

    def __init__(self, *, skills: SkillService, events: RuntimeEventBus) -> None:
        self.skills = skills
        self.events = events

    def run_review(self, db: Session, *, window_hours: int = 24, limit: int = 50) -> DreamReviewResult:
        since = datetime.now(UTC) - timedelta(hours=max(1, min(window_hours, 24 * 30)))
        memories = self._recent_error_memories(db, since=since, limit=limit)
        failed_runs = self._recent_failed_tool_runs(db, since=since, limit=limit)
        proposals: list[EvolutionProposal] = []

        groups = self._group_evidence(memories, failed_runs)
        for group_key, evidence in groups.items():
            if self._proposal_exists(db, group_key):
                continue
            proposal = self.skills.create_proposal(
                db,
                target_type="skill",
                action="upsert",
                risk_level="medium",
                payload=self._proposal_payload(group_key, evidence),
                evidence=evidence,
            )
            proposals.append(proposal)
            memory_proposal = self.skills.create_proposal(
                db,
                target_type="memory",
                action="create",
                risk_level="low",
                payload=self._memory_proposal_payload(group_key, evidence),
                evidence=evidence,
            )
            proposals.append(memory_proposal)
            wiki_proposal = self.skills.create_proposal(
                db,
                target_type="wiki",
                action="append_log",
                risk_level="low",
                payload={
                    "entry": f"Dream review found improvement opportunity `{group_key}`. Review proposal evidence before updating Wiki pages.",
                    "dream_group_key": group_key,
                },
                evidence=evidence,
            )
            proposals.append(wiki_proposal)

        for memory in self._low_stability_memories(db, since=since, limit=limit):
            group_key = f"verify-memory-{memory.id}"
            if self._proposal_exists(db, group_key, target_type="memory"):
                continue
            proposals.append(
                self.skills.create_proposal(
                    db,
                    target_type="memory",
                    action="verify",
                    risk_level="low",
                    payload={
                        "memory_id": str(memory.id),
                        "confidence_delta": 0.03,
                        "dream_group_key": group_key,
                    },
                    evidence={
                        "dream_group_key": group_key,
                        "memories": [
                            {
                                "id": str(memory.id),
                                "kind": memory.kind,
                                "content": memory.content[:1000],
                                "confidence": memory.confidence,
                                "stability": getattr(memory, "stability", None),
                            }
                        ],
                        "failed_tool_runs": [],
                    },
                )
            )

        result = DreamReviewResult(
            scanned_memories=len(memories),
            failed_tool_runs=len(failed_runs),
            proposals_created=len(proposals),
            proposal_ids=[str(item.id) for item in proposals],
        )
        self.events.emit("dream.review.completed", result.__dict__)
        return result

    def _recent_error_memories(self, db: Session, *, since: datetime, limit: int) -> list[Memory]:
        stmt = (
            select(Memory)
            .where(Memory.archived.is_(False))
            .where(Memory.created_at >= since)
            .where(Memory.kind.in_(["error_signal", "skill_trace"]))
            .order_by(desc(Memory.importance), desc(Memory.created_at))
            .limit(max(1, min(limit, 500)))
        )
        return list(db.scalars(stmt).all())

    def _recent_failed_tool_runs(self, db: Session, *, since: datetime, limit: int) -> list[ToolRun]:
        stmt = (
            select(ToolRun)
            .where(ToolRun.status == "failed")
            .where(ToolRun.started_at >= since)
            .order_by(desc(ToolRun.started_at))
            .limit(max(1, min(limit, 500)))
        )
        return list(db.scalars(stmt).all())

    def _low_stability_memories(self, db: Session, *, since: datetime, limit: int) -> list[Memory]:
        stmt = (
            select(Memory)
            .where(Memory.archived.is_(False))
            .where(Memory.updated_at >= since)
            .where(Memory.stability < 0.35)
            .order_by(desc(Memory.importance), desc(Memory.updated_at))
            .limit(max(1, min(limit, 100)))
        )
        return [item for item in db.scalars(stmt).all() if float(getattr(item, "stability", 1.0) or 1.0) < 0.35]

    def _group_evidence(self, memories: list[Memory], failed_runs: list[ToolRun]) -> dict[str, dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for memory in memories:
            key = self._group_key(memory.content, fallback=memory.kind)
            groups.setdefault(key, {"dream_group_key": key, "memories": [], "failed_tool_runs": []})
            groups[key]["memories"].append(
                {
                    "id": str(memory.id),
                    "kind": memory.kind,
                    "content": memory.content[:1000],
                    "importance": memory.importance,
                    "confidence": memory.confidence,
                    "source_turn_id": memory.source_turn_id,
                }
            )
        for run in failed_runs:
            key = self._group_key(str(run.result.get("error") or run.tool_name), fallback=run.tool_name)
            groups.setdefault(key, {"dream_group_key": key, "memories": [], "failed_tool_runs": []})
            groups[key]["failed_tool_runs"].append(
                {
                    "id": str(run.id),
                    "turn_id": run.turn_id,
                    "tool_name": run.tool_name,
                    "arguments": run.arguments,
                    "result": run.result,
                    "started_at": run.started_at.isoformat() if run.started_at else None,
                }
            )
        return {key: evidence for key, evidence in groups.items() if evidence["memories"] or evidence["failed_tool_runs"]}

    def _proposal_payload(self, group_key: str, evidence: dict[str, Any]) -> dict[str, Any]:
        title = group_key.replace("-", " ").title()
        summary = self._summary(evidence)
        skill_key = f"dream/{group_key}"
        skill_md = (
            "---\n"
            f"name: Dream Review - {title}\n"
            "description: Proposed skill distilled from recent memory error signals and failed tool runs.\n"
            "required_tools:\n"
            "  - wiki_search\n"
            "  - wiki_read\n"
            "  - memory_search\n"
            "  - memory_get\n"
            "test_cases:\n"
            "  - name: manifest-lint\n"
            "    input: Validate this generated skill proposal.\n"
            "    expected: Required checks pass before apply.\n"
            "---\n\n"
            f"# Dream Review - {title}\n\n"
            "## Trigger Evidence\n\n"
            f"{summary}\n\n"
            "## Operating Guidance\n\n"
            "- Reproduce the failure or uncertainty from the evidence before changing project state.\n"
            "- Prefer Wiki and Memory retrieval before inventing a new workflow.\n"
            "- If the issue involves tools, check availability, scope, approval requirements, and recent ToolRun errors.\n"
            "- Record any durable lesson as L2 memory first; promote to stable skill instructions only after repeated success.\n"
        )
        return {
            "skill_key": skill_key,
            "files": {"SKILL.md": skill_md},
            "dream_group_key": group_key,
            "required_checks": [
                "frontmatter",
                "required_tools",
                "static_safety_scan",
                "manifest_test_cases",
                "human_review_before_apply",
            ],
        }

    def _memory_proposal_payload(self, group_key: str, evidence: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": "semantic",
            "content": "Dream review lesson: " + self._plain_summary(evidence)[:1800],
            "source": "dream",
            "importance": 0.62,
            "confidence": 0.55,
            "stability": 0.45,
            "metadata": {"dream_group_key": group_key},
            "dream_group_key": group_key,
        }

    def _proposal_exists(self, db: Session, group_key: str, *, target_type: str = "skill") -> bool:
        stmt = (
            select(EvolutionProposal.id)
            .where(EvolutionProposal.target_type == target_type)
            .where(EvolutionProposal.status.in_(["pending", "approved", "applied"]))
            .where(EvolutionProposal.payload["dream_group_key"].as_string() == group_key)
            .limit(1)
        )
        return db.scalar(stmt) is not None

    @staticmethod
    def _group_key(text: str, *, fallback: str) -> str:
        words = re.findall(r"[a-zA-Z0-9_]+", text.lower())
        stop = {"the", "and", "for", "with", "that", "this", "from", "error", "failed", "failure"}
        useful = [word for word in words if len(word) > 2 and word not in stop][:6]
        raw = "-".join(useful) or fallback
        raw = re.sub(r"[^a-z0-9_-]+", "-", raw.lower()).strip("-")
        return raw[:80] or "general-review"

    @staticmethod
    def _summary(evidence: dict[str, Any]) -> str:
        lines: list[str] = []
        for memory in evidence.get("memories", [])[:5]:
            lines.append(f"- Memory `{memory['id']}` ({memory['kind']}): {memory['content']}")
        for run in evidence.get("failed_tool_runs", [])[:5]:
            error = run.get("result", {}).get("error") if isinstance(run.get("result"), dict) else ""
            lines.append(f"- ToolRun `{run['id']}` `{run['tool_name']}` failed: {error or run.get('result')}")
        return "\n".join(lines) or "- No evidence captured."

    @staticmethod
    def _plain_summary(evidence: dict[str, Any]) -> str:
        parts: list[str] = []
        for memory in evidence.get("memories", [])[:5]:
            parts.append(f"{memory['kind']}: {memory['content']}")
        for run in evidence.get("failed_tool_runs", [])[:5]:
            error = run.get("result", {}).get("error") if isinstance(run.get("result"), dict) else ""
            parts.append(f"{run['tool_name']} failed: {error or run.get('result')}")
        return " | ".join(parts) or "No evidence captured."
