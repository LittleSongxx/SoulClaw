"""Agent run graph persistence."""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterator

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from backend.infra.config import Settings, get_settings
from backend.infra.events import RuntimeEventBus
from backend.infra.models import AgentRun, AgentRunStep


GRAPH_NODE_ORDER = (
    "record_user",
    "plan",
    "delegate_or_context",
    "retrieve",
    "llm",
    "tools",
    "approval_or_continue",
    "finalize",
    "post_turn",
)


@dataclass(frozen=True)
class StepTimer:
    run_id: str
    step_name: str
    started: float


class AgentRunService:
    def __init__(self, *, settings: Settings | None = None, events: RuntimeEventBus | None = None) -> None:
        self.settings = settings or get_settings()
        self.events = events

    def create_run(
        self,
        db: Session,
        *,
        session_id: str,
        turn_id: str,
        input_text: str,
        route: str = "",
        state: dict[str, Any] | None = None,
    ) -> AgentRun:
        run = AgentRun(
            run_id=f"run_{uuid.uuid4().hex}",
            thread_id=session_id,
            session_id=session_id,
            turn_id=turn_id,
            engine=self.settings.agent_engine,
            status="running",
            route=route,
            input_preview=input_text[:500],
            state=state or {},
            metadata_json={"nodes": list(GRAPH_NODE_ORDER), "checkpoint": self.settings.agent_engine == "langgraph"},
        )
        db.add(run)
        db.flush()
        if self.events:
            self.events.emit("agent.run.started", {"run_id": run.run_id, "turn_id": turn_id}, turn_id=turn_id)
        return run

    @contextmanager
    def step(
        self,
        db: Session,
        run_id: str,
        step_name: str,
        *,
        input: dict[str, Any] | None = None,
    ) -> Iterator[None]:
        started = time.monotonic()
        step = self.start_step(db, run_id, step_name, input=input)
        try:
            yield
        except Exception as exc:
            self.finish_step(db, step, status="failed", error=str(exc), started=started)
            raise
        else:
            self.finish_step(db, step, status="succeeded", started=started)

    def start_step(
        self,
        db: Session,
        run_id: str,
        step_name: str,
        *,
        input: dict[str, Any] | None = None,
    ) -> AgentRunStep:
        sequence = int(
            db.scalar(select(func.count()).select_from(AgentRunStep).where(AgentRunStep.run_id == run_id))
            or 0
        )
        step = AgentRunStep(
            run_id=run_id,
            step_name=step_name,
            sequence=sequence,
            status="running",
            input=input or {},
            output={},
        )
        db.add(step)
        db.flush()
        return step

    def finish_step(
        self,
        db: Session,
        step: AgentRunStep,
        *,
        status: str = "succeeded",
        output: dict[str, Any] | None = None,
        error: str = "",
        started: float | None = None,
    ) -> AgentRunStep:
        step.status = status
        step.output = output or step.output or {}
        step.error = error[:4000]
        step.finished_at = datetime.now(UTC)
        if started is not None:
            step.output = {**(step.output or {}), "duration_ms": int((time.monotonic() - started) * 1000)}
        db.flush()
        return step

    def add_step(
        self,
        db: Session,
        run_id: str,
        step_name: str,
        *,
        status: str = "succeeded",
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        error: str = "",
    ) -> AgentRunStep:
        step = self.start_step(db, run_id, step_name, input=input)
        return self.finish_step(db, step, status=status, output=output, error=error)

    def finish_run(
        self,
        db: Session,
        run_id: str,
        *,
        status: str,
        answer: str = "",
        error: str = "",
        state: dict[str, Any] | None = None,
    ) -> AgentRun | None:
        run = db.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
        if run is None:
            return None
        run.status = status
        run.answer_preview = answer[:500]
        run.error = error[:4000]
        if state is not None:
            run.state = state
        run.finished_at = datetime.now(UTC)
        db.flush()
        if self.events:
            self.events.emit("agent.run.finished", {"run_id": run.run_id, "status": status}, turn_id=run.turn_id)
        return run

    def list_runs(self, db: Session, *, limit: int = 100) -> list[AgentRun]:
        limit = max(1, min(limit, 500))
        return list(db.scalars(select(AgentRun).order_by(desc(AgentRun.started_at)).limit(limit)).all())

    def graph(self, db: Session, run_id: str) -> dict[str, Any]:
        run = db.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
        if run is None:
            raise KeyError(f"agent run not found: {run_id}")
        steps = list(
            db.scalars(
                select(AgentRunStep)
                .where(AgentRunStep.run_id == run_id)
                .order_by(AgentRunStep.sequence, AgentRunStep.started_at)
            ).all()
        )
        nodes = [
            {
                "id": step.step_name,
                "sequence": step.sequence,
                "status": step.status,
                "input": step.input or {},
                "output": step.output or {},
                "error": step.error,
                "started_at": step.started_at.isoformat() if step.started_at else None,
                "finished_at": step.finished_at.isoformat() if step.finished_at else None,
            }
            for step in steps
        ]
        edges = [
            {"source": nodes[index]["id"], "target": nodes[index + 1]["id"]}
            for index in range(len(nodes) - 1)
        ]
        return {
            "run": {
                "id": str(run.id),
                "run_id": run.run_id,
                "thread_id": run.thread_id,
                "session_id": run.session_id,
                "turn_id": run.turn_id,
                "engine": run.engine,
                "status": run.status,
                "route": run.route,
                "input_preview": run.input_preview,
                "answer_preview": run.answer_preview,
                "error": run.error,
                "state": run.state or {},
                "metadata": run.metadata_json or {},
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            },
            "nodes": nodes,
            "edges": edges,
        }
