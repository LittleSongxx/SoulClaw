"""Heartbeat review task for proactive long-term assistant upkeep."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from backend.domain.skills import SkillService
from backend.domain.workspace import WorkspaceService
from backend.infra.events import RuntimeEventBus


@dataclass(frozen=True)
class HeartbeatResult:
    status: str
    active_tasks: int
    proposals_created: int
    proposal_ids: list[str]


class HeartbeatRuntime:
    def __init__(self, *, workspace: WorkspaceService, skills: SkillService, events: RuntimeEventBus) -> None:
        self.workspace = workspace
        self.skills = skills
        self.events = events

    def run_check(self, db: Session) -> HeartbeatResult:
        tasks = self.workspace.active_heartbeat_tasks()
        if not tasks:
            result = HeartbeatResult(status="skipped", active_tasks=0, proposals_created=0, proposal_ids=[])
            self.workspace.append_history({"type": "heartbeat", "status": "skipped", "reason": "no active tasks"})
            self.events.emit("heartbeat.skipped", {"reason": "no active tasks"})
            return result

        proposal_ids: list[str] = []
        for index, task in enumerate(tasks, start=1):
            proposal = self.skills.create_proposal(
                db,
                target_type="heartbeat",
                action="append_file",
                risk_level="low",
                payload={
                    "kind": "heartbeat",
                    "content": f"- Reviewed active task {index}: {task}\n  - Status: pending human review.",
                    "heartbeat_task": task,
                },
                evidence={"heartbeat_task": task, "source": "HEARTBEAT.md"},
            )
            proposal_ids.append(str(proposal.id))
        self.workspace.append_history(
            {"type": "heartbeat", "status": "proposed", "active_tasks": tasks, "proposal_ids": proposal_ids}
        )
        result = HeartbeatResult(
            status="proposed",
            active_tasks=len(tasks),
            proposals_created=len(proposal_ids),
            proposal_ids=proposal_ids,
        )
        self.events.emit("heartbeat.completed", result.__dict__)
        return result
