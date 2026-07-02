"""Proposal-first long-term memory curator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from backend.domain.core_context import CoreContextService
from backend.domain.evolution_proposals import EvolutionProposalService
from backend.domain.memory import MemoryService
from backend.infra.events import RuntimeEventBus
from backend.infra.models import EvolutionProposal


@dataclass(frozen=True)
class CuratorResult:
    proposals: list[EvolutionProposal]
    created: int


class MemoryCuratorService:
    """Creates reviewable proposals from durable signals, never direct writes."""

    def __init__(
        self,
        *,
        memory: MemoryService,
        core_context: CoreContextService,
        proposals: EvolutionProposalService,
        events: RuntimeEventBus | None = None,
    ) -> None:
        self.memory = memory
        self.core_context = core_context
        self.proposals = proposals
        self.events = events

    def curate_turn(
        self,
        db: Session,
        *,
        session_id: str,
        turn_id: str,
        user_message: str,
        assistant_answer: str,
        tool_results: list[dict[str, Any]] | None = None,
        a2a_result: dict[str, Any] | None = None,
    ) -> CuratorResult:
        proposals: list[EvolutionProposal] = []
        tool_results = tool_results or []
        a2a_result = a2a_result or {}
        evidence_base = {
            "source": "curator.post_turn",
            "session_id": session_id,
            "turn_id": turn_id,
            "user_message": user_message[:1200],
            "assistant_answer": assistant_answer[:1200],
        }
        explicit = self._explicit_memory_request(user_message)
        if explicit:
            proposals.append(
                self.proposals.create(
                    db,
                    target_type="memory",
                    action="create",
                    payload={
                        "kind": "user_fact",
                        "content": explicit,
                        "source": "curator",
                        "importance": 0.72,
                        "confidence": 0.7,
                        "stability": 0.65,
                        "metadata": {"source_turn_id": turn_id, "session_id": session_id, "extraction": "explicit_user_request"},
                    },
                    evidence=evidence_base | {"reason": "user explicitly requested durable memory"},
                    risk_level="medium",
                )
            )
        for result in tool_results:
            if str(result.get("status") or "").lower() == "failed":
                proposals.append(
                    self.proposals.create(
                        db,
                        target_type="memory",
                        action="create",
                        payload={
                            "kind": "error_signal",
                            "content": self._tool_failure_content(result),
                            "source": "curator",
                            "importance": 0.58,
                            "confidence": 0.65,
                            "stability": 0.35,
                            "metadata": {"source_turn_id": turn_id, "session_id": session_id, "tool_name": result.get("tool_name")},
                        },
                        evidence=evidence_base | {"tool_result": result},
                        risk_level="low",
                    )
                )
        if a2a_result and a2a_result.get("status") in {"completed", "failed"}:
            proposals.append(
                self.proposals.create(
                    db,
                    target_type="memory",
                    action="create",
                    payload={
                        "kind": "project_knowledge" if a2a_result.get("status") == "completed" else "error_signal",
                        "content": self._a2a_content(a2a_result),
                        "source": "a2a",
                        "importance": 0.55,
                        "confidence": 0.55,
                        "stability": 0.4,
                        "metadata": {"source_turn_id": turn_id, "session_id": session_id, "a2a_task_id": a2a_result.get("task_id")},
                    },
                    evidence=evidence_base | {"a2a": a2a_result},
                    risk_level="low",
                )
            )
        if proposals and self.events:
            self.events.emit("memory.curator.proposals_created", {"turn_id": turn_id, "created": len(proposals)})
        return CuratorResult(proposals=proposals, created=len(proposals))

    @staticmethod
    def _explicit_memory_request(message: str) -> str:
        text = " ".join((message or "").split())
        lowered = text.lower()
        markers = ["remember that", "please remember", "记住", "请记住", "以后记得"]
        if not any(marker in lowered or marker in text for marker in markers):
            return ""
        for marker in markers:
            index = lowered.find(marker) if marker.isascii() else text.find(marker)
            if index >= 0:
                return text[index + len(marker) :].strip(" :，,。")[:1200] or text[:1200]
        return text[:1200]

    @staticmethod
    def _tool_failure_content(result: dict[str, Any]) -> str:
        tool_name = str(result.get("tool_name") or "unknown_tool")
        error = str(result.get("error") or result.get("result") or "tool failed")
        return f"Tool `{tool_name}` failed during a turn: {error[:900]}"

    @staticmethod
    def _a2a_content(result: dict[str, Any]) -> str:
        task_id = str(result.get("task_id") or "")
        status = str(result.get("status") or "")
        answer = str(result.get("answer") or result.get("error") or "")[:1000]
        return f"A2A task `{task_id}` finished with status `{status}`. Summary: {answer}"
