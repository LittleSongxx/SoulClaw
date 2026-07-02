"""Dream/self-evolution review jobs for conservative skill proposals."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from backend.domain.evolution_proposals import EvolutionProposalService
from backend.infra.events import RuntimeEventBus
from backend.infra.models import EvolutionProposal, Memory, ToolRun


@dataclass(frozen=True)
class DreamReviewResult:
    scanned_memories: int
    failed_tool_runs: int
    proposals_created: int
    proposal_ids: list[str]
    learning_candidates: int = 0
    skill_candidates: int = 0
    skipped_skill_candidates: int = 0


class DreamRuntime:
    """Turns error signals and failed tool runs into reviewable proposals.

    It never writes skill files. The output is a pending evolution proposal
    with evidence, required checks, and a draft SKILL.md that must still pass
    lint/test before apply.
    """

    def __init__(self, *, proposals: EvolutionProposalService, events: RuntimeEventBus) -> None:
        self.proposals = proposals
        self.events = events

    def run_review(self, db: Session, *, window_hours: int = 24, limit: int = 50) -> DreamReviewResult:
        since = datetime.now(UTC) - timedelta(hours=max(1, min(window_hours, 24 * 30)))
        memories = self._recent_error_memories(db, since=since, limit=limit)
        failed_runs = self._recent_failed_tool_runs(db, since=since, limit=limit)
        proposals: list[EvolutionProposal] = []

        groups = self._group_evidence(memories, failed_runs)
        for group_key, evidence in groups.items():
            evidence["learning_candidate"] = self._learning_candidate(group_key, evidence)
            if self._should_propose_skill(evidence):
                if not self._proposal_exists(db, group_key):
                    proposal = self.proposals.create(
                        db,
                        target_type="skill",
                        action="upsert",
                        risk_level="medium",
                        payload=self._proposal_payload(group_key, evidence),
                        evidence=evidence,
                    )
                    proposals.append(proposal)
            if not self._proposal_exists(db, group_key, target_type="memory"):
                memory_proposal = self.proposals.create(
                    db,
                    target_type="memory",
                    action="create",
                    risk_level="low",
                    payload=self._memory_proposal_payload(group_key, evidence),
                    evidence=evidence,
                )
                proposals.append(memory_proposal)
            if not self._proposal_exists(db, group_key, target_type="wiki"):
                wiki_proposal = self.proposals.create(
                    db,
                    target_type="wiki",
                    action="append_log",
                    risk_level="low",
                    payload={
                        "entry": (
                            f"Dream review recorded learning candidate `{group_key}`. "
                            "Promote to Skill only after repeated evidence and review."
                        ),
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
                self.proposals.create(
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
            learning_candidates=len(groups),
            skill_candidates=sum(1 for evidence in groups.values() if self._should_propose_skill(evidence)),
            skipped_skill_candidates=sum(1 for evidence in groups.values() if not self._should_propose_skill(evidence)),
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

    def _learning_candidate(self, group_key: str, evidence: dict[str, Any]) -> dict[str, Any]:
        memory_count = len(evidence.get("memories", []) or [])
        failed_run_count = len(evidence.get("failed_tool_runs", []) or [])
        support_count = memory_count + failed_run_count
        distinct_tools = sorted(
            {
                str(item.get("tool_name") or "")
                for item in evidence.get("failed_tool_runs", [])
                if str(item.get("tool_name") or "").strip()
            }
        )
        has_skill_trace = any(item.get("kind") == "skill_trace" for item in evidence.get("memories", []))
        should_promote = support_count >= 2 or failed_run_count >= 2 or (has_skill_trace and failed_run_count >= 1)
        if support_count >= 3:
            strength = "strong"
        elif should_promote:
            strength = "moderate"
        else:
            strength = "weak"
        return {
            "id": group_key,
            "support_count": support_count,
            "memory_count": memory_count,
            "failed_tool_run_count": failed_run_count,
            "distinct_tools": distinct_tools,
            "strength": strength,
            "promote_to_skill": should_promote,
            "recommended_target": "skill" if should_promote else "memory",
            "reason": (
                "Repeated or mixed evidence supports procedural skill review."
                if should_promote
                else "Single weak signal should stay as memory/wiki review before becoming a skill."
            ),
        }

    def _should_propose_skill(self, evidence: dict[str, Any]) -> bool:
        candidate = evidence.get("learning_candidate")
        if not isinstance(candidate, dict):
            return False
        return bool(candidate.get("promote_to_skill"))

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
            "    kind: lint\n"
            "  - name: required retrieval tools\n"
            "    kind: mock_tool\n"
            "    required_tools:\n"
            "      - wiki_search\n"
            "      - wiki_read\n"
            "      - memory_search\n"
            "      - memory_get\n"
            "  - name: regression unsafe commands absent\n"
            "    kind: regression\n"
            "    must_not_contain:\n"
            "      - git reset --hard\n"
            "      - rm -rf /\n"
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
        candidate = evidence.get("learning_candidate") if isinstance(evidence.get("learning_candidate"), dict) else {}
        return {
            "skill_key": skill_key,
            "files": {"SKILL.md": skill_md},
            "dream_group_key": group_key,
            "learning_candidate": candidate,
            "required_checks": [
                "frontmatter",
                "required_tools",
                "static_safety_scan",
                "safe_manifest_test_cases",
                "human_review_before_apply",
            ],
        }

    def _memory_proposal_payload(self, group_key: str, evidence: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": "semantic",
            "content": "Dream learning candidate: " + self._plain_summary(evidence)[:1800],
            "source": "dream",
            "importance": 0.62 if self._should_propose_skill(evidence) else 0.52,
            "confidence": 0.55,
            "stability": 0.45 if self._should_propose_skill(evidence) else 0.35,
            "metadata": {
                "dream_group_key": group_key,
                "learning_candidate": evidence.get("learning_candidate", {}),
            },
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
