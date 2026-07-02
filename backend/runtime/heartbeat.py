"""Heartbeat review task for proactive long-term assistant upkeep."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from backend.domain.core_context import CoreContextService
from backend.domain.evolution_proposals import EvolutionProposalService
from backend.infra.events import RuntimeEventBus


@dataclass(frozen=True)
class HeartbeatResult:
    status: str
    active_tasks: int
    proposals_created: int
    proposal_ids: list[str]


class HeartbeatRuntime:
    def __init__(self, *, core_context: CoreContextService, proposals: EvolutionProposalService, events: RuntimeEventBus) -> None:
        self.core_context = core_context
        self.proposals = proposals
        self.events = events

    def run_check(self, db: Session) -> HeartbeatResult:
        tasks = self.core_context.heartbeat_tasks(db)
        if not tasks:
            result = HeartbeatResult(status="skipped", active_tasks=0, proposals_created=0, proposal_ids=[])
            self.events.emit("heartbeat.skipped", {"reason": "no active structured tasks"})
            return result

        proposal_ids: list[str] = []
        for index, task in enumerate(tasks, start=1):
            proposal = self.proposals.create(
                db,
                target_type="core_context",
                action="update",
                risk_level="low",
                payload={
                    "block_key": "heartbeat",
                    "content": f"Reviewed active task {index}: {task}\nStatus: pending human review.",
                    "heartbeat_task": task,
                },
                evidence={"heartbeat_task": task, "source": "core_context"},
            )
            proposal_ids.append(str(proposal.id))
        result = HeartbeatResult(
            status="proposed",
            active_tasks=len(tasks),
            proposals_created=len(proposal_ids),
            proposal_ids=proposal_ids,
        )
        self.events.emit("heartbeat.completed", result.__dict__)
        return result
