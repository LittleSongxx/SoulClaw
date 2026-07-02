"""Agent turn runtime with graph/run/checkpoint integration."""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from backend.domain.conversation import ConversationService, SessionContext
from backend.domain.core_context import CoreContextService
from backend.domain.memory import MemoryService
from backend.domain.memory_curator import MemoryCuratorService
from backend.domain.runs import AgentRunService
from backend.domain.tools import ToolApprovalRequired, ToolExecutor, ToolRegistry
from backend.domain.wiki import WikiService
from backend.infra.events import RuntimeEventBus
from backend.infra.models import Approval
from backend.infra.rate_limit import RateLimitExceeded
from backend.infra.resilience import CircuitOpenError
from backend.infra.trace import current_trace_id
from backend.runtime.a2a import A2ADelegateRequest, A2ARuntimeManager
from backend.runtime.checkpoint import AgentCheckpointStore
from backend.runtime.langgraph_runtime import AgentTurnGraphShell
from backend.runtime.llm import OpenAICompatibleClient


@dataclass(frozen=True)
class AgentTurnResult:
    turn_id: str
    answer: str
    context: dict[str, Any]
    status: str = "completed"
    approval_id: str = ""
    pending_tool_call: dict[str, Any] | None = None
    resume_available: bool = False


@dataclass(frozen=True)
class AgentTaskPlan:
    """Compact routing contract for the local-first assistant turn."""

    route: str
    reason: str
    memory_policy: str
    wiki_policy: str
    skill_policy: str
    delegation: dict[str, Any] | None = None

    def to_context(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "reason": self.reason,
            "memory_policy": self.memory_policy,
            "wiki_policy": self.wiki_policy,
            "skill_policy": self.skill_policy,
            "delegation": self.delegation or {},
        }


class AgentApprovalInterrupt(Exception):
    def __init__(
        self,
        *,
        approval_id: str,
        pending_tool_call: dict[str, Any],
        completed_results: list[dict[str, Any]],
        approval_result: dict[str, Any],
        pending_index: int,
    ) -> None:
        super().__init__("agent turn requires approval")
        self.approval_id = approval_id
        self.pending_tool_call = pending_tool_call
        self.completed_results = completed_results
        self.approval_result = approval_result
        self.pending_index = pending_index
        self.messages: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.tool_results: list[dict[str, Any]] = []
        self.round_tool_calls: list[dict[str, Any]] = []
        self.round_index = 0


class AgentRuntime:
    """Run one assistant turn while preserving the public run/resume contract."""

    def __init__(
        self,
        wiki: WikiService,
        memory: MemoryService,
        events: RuntimeEventBus,
        tools: ToolExecutor | None = None,
        registry: ToolRegistry | None = None,
        llm: OpenAICompatibleClient | None = None,
        conversation: ConversationService | None = None,
        core_context: CoreContextService | None = None,
        memory_curator: MemoryCuratorService | None = None,
        a2a: A2ARuntimeManager | None = None,
        runs: AgentRunService | None = None,
        checkpoints: AgentCheckpointStore | None = None,
    ) -> None:
        self.wiki = wiki
        self.memory = memory
        self.events = events
        self.tools = tools
        self.registry = registry
        self.llm = llm
        self.conversation = conversation
        self.core_context = core_context
        self.memory_curator = memory_curator
        self.a2a = a2a
        self.runs = runs
        self.graph_shell = AgentTurnGraphShell()
        self.checkpoints = checkpoints

    def run_turn(
        self,
        db,
        message: str,
        *,
        session_id: str = "local",
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> AgentTurnResult:
        turn_id = str(uuid.uuid4())
        agent_run_id = ""
        if self.runs is not None:
            agent_run = self.runs.create_run(
                db,
                session_id=session_id,
                turn_id=turn_id,
                input_text=message,
                state={"graph": self.graph_shell.state()},
            )
            agent_run_id = agent_run.run_id
        self._save_graph_checkpoint(
            thread_id=session_id,
            run_id=agent_run_id,
            checkpoint_id=f"{turn_id}:start",
            state={"node": "record_user", "turn_id": turn_id, "message": message[:1000]},
        )
        self.events.emit("turn.started", {"message_preview": message[:200]}, session_id=session_id, turn_id=turn_id)
        session_context = (
            self.conversation.recent_context(db, session_id, limit=12)
            if self.conversation is not None
            else SessionContext(summary="", messages=[], total_messages=0)
        )
        if self.conversation is not None:
            self.conversation.record_user_message(db, session_id=session_id, turn_id=turn_id, content=message)
        self._record_run_step(
            db,
            agent_run_id,
            "record_user",
            output={"session_id": session_id, "conversation_messages": session_context.total_messages},
        )
        task_plan = self._plan_turn(message, tool_calls=tool_calls)
        self.events.emit(
            "agent.node.turn_planned",
            task_plan.to_context(),
            session_id=session_id,
            turn_id=turn_id,
        )
        self._record_run_step(db, agent_run_id, "plan", output=task_plan.to_context())
        delegated_result: dict[str, Any] | None = None
        a2a_delegate_error: dict[str, Any] | None = None
        if tool_calls is None and self.tools is not None and task_plan.route == "deep_research":
            try:
                delegation = task_plan.delegation or {}
                delegated_result = self.tools.execute(
                    db,
                    tool_name="a2a_delegate",
                    arguments={
                        "capability": str(delegation.get("capability") or "deep-research"),
                        "query": message,
                        "context": {
                            "session_id": session_id,
                            "turn_id": turn_id,
                            "task_plan": task_plan.to_context(),
                        },
                        "options": delegation.get("options") if isinstance(delegation.get("options"), dict) else {},
                        "connection_name": str(delegation.get("connection_name") or ""),
                    },
                    turn_id=turn_id,
                )
                delegated_payload = delegated_result.get("result") if isinstance(delegated_result.get("result"), dict) else delegated_result
                if delegated_payload.get("status") in {"failed", "canceled", "cancelled", "rejected"}:
                    a2a_delegate_error = delegated_payload
                    delegated_result = None
                    self.events.emit(
                        "a2a.delegate.failed",
                        {
                            "status": a2a_delegate_error.get("status"),
                            "error": a2a_delegate_error.get("error") or a2a_delegate_error.get("answer") or "",
                            "message_preview": message[:200],
                            "fallback": "local_llm",
                        },
                        severity="warning",
                        session_id=session_id,
                        turn_id=turn_id,
                    )
                else:
                    delegated_result = delegated_payload
            except ToolApprovalRequired as exc:
                checkpoint = {
                    "mode": "agent_turn",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "message": message,
                    "messages": [],
                    "tool_calls": [],
                    "tool_results": [],
                    "round_tool_calls": [],
                    "round_completed_results": [],
                    "pending_tool_call": {
                        "name": "a2a_delegate",
                        "arguments": {
                            "capability": str((task_plan.delegation or {}).get("capability") or "deep-research"),
                            "query": message,
                            "context": {
                                "session_id": session_id,
                                "turn_id": turn_id,
                                "task_plan": task_plan.to_context(),
                            },
                            "options": (task_plan.delegation or {}).get("options")
                            if isinstance((task_plan.delegation or {}).get("options"), dict)
                            else {},
                            "connection_name": str((task_plan.delegation or {}).get("connection_name") or ""),
                        },
                    },
                    "pending_index": 0,
                    "round_index": 0,
                    "run_id": agent_run_id,
                    "thread_id": session_id,
                }
                self._save_approval_checkpoint(db, exc.approval_id, checkpoint)
                self._save_graph_checkpoint(
                    thread_id=session_id,
                    run_id=agent_run_id,
                    checkpoint_id=f"{turn_id}:approval:{exc.approval_id}",
                    state=checkpoint,
                )
                self._record_run_step(
                    db,
                    agent_run_id,
                    "approval_or_continue",
                    status="approval_required",
                    output={"approval_id": exc.approval_id, "pending_tool_call": checkpoint["pending_tool_call"]},
                )
                answer = "Approval is required before I can delegate this turn."
                context = self._approval_context(
                    session_id=session_id,
                    agent_run_id=agent_run_id,
                    task_plan=task_plan,
                    approval_id=exc.approval_id,
                    pending_tool_call=checkpoint["pending_tool_call"],
                )
                self._finish_run(db, agent_run_id, status="approval_required", answer=answer, state={"context": context})
                return AgentTurnResult(
                    turn_id=turn_id,
                    answer=answer,
                    context=context,
                    status="approval_required",
                    approval_id=exc.approval_id,
                    pending_tool_call=checkpoint["pending_tool_call"],
                    resume_available=True,
                )
            except Exception as exc:  # noqa: BLE001
                a2a_delegate_error = {"status": "failed", "error": str(exc)}
                self.events.emit(
                    "a2a.delegate.failed",
                    {
                        "error": str(exc),
                        "message_preview": message[:200],
                        "fallback": "local_llm",
                    },
                    severity="warning",
                    session_id=session_id,
                    turn_id=turn_id,
                )
        self._record_run_step(
            db,
            agent_run_id,
            "delegate_or_context",
            output={
                "delegated": delegated_result is not None,
                "a2a_error": bool(a2a_delegate_error),
                "route": task_plan.route,
            },
        )
        wiki_orientation = self.wiki.orientation(db) if hasattr(self.wiki, "orientation") else {"pages": [], "index": "", "schema": "", "recent_log": ""}
        wiki_hits = self.wiki.search(db, message, limit=5) if self.llm is None or not self.llm.configured else []
        memory_context = self.memory.resident_context(db, message, dynamic_limit=5)
        skill_index = self._skill_index(db, message)
        core_context = self.core_context.prompt_context(db) if self.core_context is not None else {}
        self.events.emit(
            "agent.node.context_assembled",
            {
                "wiki_pages": wiki_orientation.get("page_count", 0),
                "resident_memory": len(memory_context.get("resident", [])),
                "dynamic_memory": len(memory_context.get("dynamic", [])),
                "core_blocks": len([item for item in core_context.values() if item is not None]),
                "skills": len(skill_index),
            },
            session_id=session_id,
            turn_id=turn_id,
        )
        self._record_run_step(
            db,
            agent_run_id,
            "retrieve",
            output={
                "wiki_pages": wiki_orientation.get("page_count", 0),
                "wiki_hits": len(wiki_hits),
                "resident_memory": len(memory_context.get("resident", [])),
                "dynamic_memory": len(memory_context.get("dynamic", [])),
                "core_blocks": len([item for item in core_context.values() if item is not None]),
                "skills": len(skill_index),
            },
        )
        tool_results: list[dict[str, Any]] = []
        inferred_tool_calls: list[dict[str, Any]] = []
        llm_answer = ""
        llm_status = "not_configured"
        turn_status = "completed"
        approval_id = ""
        pending_tool_call: dict[str, Any] | None = None
        resume_available = False
        if delegated_result is not None:
            llm_answer = self._delegated_answer(delegated_result)
            llm_status = "delegated_a2a"
        elif tool_calls is None:
            try:
                llm_answer, inferred_tool_calls, llm_status, llm_messages, tool_results = self._run_llm(
                    db,
                    turn_id,
                    session_id,
                    message,
                    wiki_orientation,
                    memory_context,
                    skill_index,
                    session_context,
                    core_context,
                    task_plan,
                )
            except AgentApprovalInterrupt as interrupt:
                checkpoint = {
                    "mode": "agent_turn",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "message": message,
                    "messages": interrupt.messages,
                    "tool_calls": interrupt.tool_calls,
                    "tool_results": interrupt.tool_results,
                    "round_tool_calls": interrupt.round_tool_calls,
                    "round_completed_results": interrupt.completed_results,
                    "pending_tool_call": interrupt.pending_tool_call,
                    "pending_index": interrupt.pending_index,
                    "round_index": interrupt.round_index,
                    "run_id": agent_run_id,
                    "thread_id": session_id,
                }
                self._save_approval_checkpoint(db, interrupt.approval_id, checkpoint)
                self._save_graph_checkpoint(
                    thread_id=session_id,
                    run_id=agent_run_id,
                    checkpoint_id=f"{turn_id}:approval:{interrupt.approval_id}",
                    state=checkpoint,
                )
                inferred_tool_calls = interrupt.tool_calls
                tool_results = interrupt.tool_results
                llm_status = "approval_required"
                turn_status = "approval_required"
                approval_id = interrupt.approval_id
                pending_tool_call = interrupt.pending_tool_call
                resume_available = bool(approval_id)
                llm_answer = "Approval is required before I can continue this turn."
                self.events.emit(
                    "agent.node.approval_interrupt",
                    {"approval_id": approval_id, "tool_name": pending_tool_call.get("name") if pending_tool_call else ""},
                    severity="warning",
                    session_id=session_id,
                    turn_id=turn_id,
                )
                self._record_run_step(
                    db,
                    agent_run_id,
                    "approval_or_continue",
                    status="approval_required",
                    output={"approval_id": approval_id, "pending_tool_call": pending_tool_call or {}},
                )
        if self.tools is not None and tool_calls is not None:
            for call in tool_calls if tool_calls is not None else inferred_tool_calls:
                try:
                    result = self.tools.execute(
                        db,
                        tool_name=str(call.get("name") or call.get("tool_name") or ""),
                        arguments=call.get("arguments") if isinstance(call.get("arguments"), dict) else {},
                        turn_id=turn_id,
                        approved=bool(call.get("approved", False)),
                    )
                    tool_results.append(result)
                except ToolApprovalRequired as exc:
                    approval_id = exc.approval_id
                    pending_tool_call = dict(call)
                    turn_status = "approval_required"
                    resume_available = False
                    tool_results.append(
                        {
                            "status": "approval_required",
                            "error": str(exc),
                            "tool_name": call.get("name") or call.get("tool_name"),
                            "approval_id": approval_id,
                        }
                    )
                except PermissionError as exc:
                    tool_results.append({"status": "approval_required", "error": str(exc), "tool_name": call.get("name") or call.get("tool_name")})
                except Exception as exc:  # noqa: BLE001
                    tool_results.append({"status": "failed", "error": str(exc), "tool_name": call.get("name") or call.get("tool_name")})
                if self.conversation is not None and tool_results:
                    self.conversation.record_tool_message(
                        db,
                        session_id=session_id,
                        turn_id=turn_id,
                        content=json.dumps(tool_results[-1], ensure_ascii=False, default=str)[:4000],
                        metadata={"tool_name": call.get("name") or call.get("tool_name")},
                    )
        self._record_run_step(
            db,
            agent_run_id,
            "llm",
            status="approval_required" if llm_status == "approval_required" else "succeeded",
            output={"status": llm_status, "tool_calls": len(inferred_tool_calls), "delegated": delegated_result is not None},
        )
        self._record_run_step(
            db,
            agent_run_id,
            "tools",
            status="approval_required" if turn_status == "approval_required" else "succeeded",
            output={"results": len(tool_results), "pending_tool_call": pending_tool_call or {}},
        )
        if turn_status != "approval_required":
            self._record_run_step(db, agent_run_id, "approval_or_continue", output={"status": "continued"})
        answer = llm_answer or self._degraded_answer(llm_status) or self._fallback_answer(wiki_hits, memory_context, tool_results)
        self.events.emit(
            "agent.node.final_answer",
            {"llm_status": llm_status, "tool_results": len(tool_results), "answer_preview": answer[:200]},
            session_id=session_id,
            turn_id=turn_id,
        )
        self._record_run_step(
            db,
            agent_run_id,
            "finalize",
            status="approval_required" if turn_status == "approval_required" else "succeeded",
            output={"answer_preview": answer[:200], "llm_status": llm_status},
        )
        wiki_read_used = any(item.get("tool_name") == "wiki_read" and item.get("status") == "succeeded" for item in tool_results)
        wiki_search_used = any(item.get("tool_name") in {"wiki_search", "wiki_orient", "wiki_follow_links"} for item in tool_results)
        if wiki_search_used and not wiki_read_used:
            self.events.emit(
                "wiki.retrieval_guard.warning",
                {"reason": "wiki index lookup occurred without wiki_read evidence"},
                severity="warning",
                session_id=session_id,
                turn_id=turn_id,
            )
        if self.conversation is not None:
            self.conversation.record_assistant_message(
                db,
                session_id=session_id,
                turn_id=turn_id,
                content=answer,
                metadata={"llm_status": llm_status},
            )
            self.conversation.update_summary_if_needed(db, session_id=session_id, llm=self.llm)
        curator_summary = {"created": 0, "proposal_ids": []}
        if turn_status != "approval_required":
            curator_summary = self._curate_turn(
                db,
                session_id=session_id,
                turn_id=turn_id,
                message=message,
                answer=answer,
                tool_results=tool_results,
                a2a_result=delegated_result or a2a_delegate_error or {},
            )
        context = {
            "wiki": [
                {
                    "page_key": item["page"].page_key,
                    "title": item["page"].title,
                    "source": item["source"],
                    "score": item["score"],
                    "summary": item.get("summary") or item["page"].summary,
                    "path": item.get("path") or item["page"].path,
                }
                for item in wiki_hits
            ],
            "memory": {
                "resident": [str(item.id) for item in memory_context["resident"]],
                "dynamic": [str(item.id) for item in memory_context["dynamic"]],
            },
            "conversation": {
                "summary": bool(session_context.summary),
                "recent_messages": len(session_context.messages),
                "total_messages": session_context.total_messages,
            },
            "wiki_orientation": {
                "page_count": wiki_orientation.get("page_count", 0),
                "canonical_files": wiki_orientation.get("root") and bool(wiki_orientation.get("index")),
            },
            "core_context": {
                kind: {
                    "id": str(item.id),
                    "title": item.title,
                    "version": item.version,
                    "confidence": item.confidence,
                    "status": item.status,
                }
                for kind, item in core_context.items()
                if item is not None
            },
            "skills": {
                "indexed": len(skill_index),
                "items": [{"skill_key": item.get("skill_key"), "name": item.get("name")} for item in skill_index[:8]],
            },
            "tools": tool_results,
            "a2a": delegated_result or a2a_delegate_error or {},
            "memory_curator": curator_summary,
            "task_plan": task_plan.to_context(),
            "llm": {"status": llm_status, "tool_calls": inferred_tool_calls, "wiki_read_used": wiki_read_used},
            "trace": {"trace_id": current_trace_id()},
            "turn": {
                "status": turn_status,
                "approval_id": approval_id,
                "pending_tool_call": pending_tool_call or {},
                "resume_available": resume_available,
            },
            "run": {
                "run_id": agent_run_id,
                "thread_id": session_id,
                "graph": self.graph_shell.state(),
            },
        }
        event_type = "turn.approval_required" if turn_status == "approval_required" else "turn.completed"
        self.events.emit(event_type, {"context": context}, session_id=session_id, turn_id=turn_id)
        self.events.emit(
            "agent.node.post_turn",
            {"summary_checked": self.conversation is not None, "curator_proposals": curator_summary.get("created", 0)},
            session_id=session_id,
            turn_id=turn_id,
        )
        self._record_run_step(db, agent_run_id, "post_turn", output={"event_type": event_type, "curator": curator_summary})
        self._finish_run(
            db,
            agent_run_id,
            status=turn_status,
            answer=answer,
            state={"context": {"approval_id": approval_id, "pending_tool_call": pending_tool_call or {}}},
        )
        return AgentTurnResult(
            turn_id=turn_id,
            answer=answer,
            context=context,
            status=turn_status,
            approval_id=approval_id,
            pending_tool_call=pending_tool_call,
            resume_available=resume_available,
        )

    def resume_turn(
        self,
        db,
        approval_id: str | uuid.UUID,
        *,
        decision: str = "approve",
        edited_arguments: dict[str, Any] | None = None,
        response: str = "",
    ) -> AgentTurnResult:
        approval = db.get(Approval, uuid.UUID(str(approval_id)))
        if approval is None:
            raise KeyError(f"approval not found: {approval_id}")
        checkpoint = approval.turn_checkpoint or {}
        if checkpoint.get("mode") != "agent_turn":
            raise ValueError("approval does not have an agent turn checkpoint")
        session_id = str(checkpoint.get("session_id") or "local")
        turn_id = str(checkpoint.get("turn_id") or "")
        agent_run_id = str(checkpoint.get("run_id") or (approval.resume_state or {}).get("run_id") or "")
        pending_tool_call = checkpoint.get("pending_tool_call") if isinstance(checkpoint.get("pending_tool_call"), dict) else {}
        if not turn_id or not pending_tool_call:
            raise ValueError("approval checkpoint is missing turn_id or pending_tool_call")
        self._record_run_step(
            db,
            agent_run_id,
            "approval_resume",
            output={"approval_id": str(approval.id), "decision": decision},
        )
        self._save_graph_checkpoint(
            thread_id=session_id,
            run_id=agent_run_id,
            checkpoint_id=f"{turn_id}:resume:{approval.id}",
            state={"checkpoint": checkpoint, "decision": decision},
        )

        normalized_decision = decision.strip().lower() or "approve"
        if normalized_decision not in {"approve", "edit", "reject", "respond"}:
            raise ValueError("decision must be approve, edit, reject, or respond")
        if normalized_decision == "edit" and edited_arguments:
            approval.edited_arguments = edited_arguments
        if normalized_decision == "respond":
            answer = response or str((approval.resume_state or {}).get("response") or "Handled by human response.")
            approval.status = "cancelled"
            approval.resolved_at = datetime.now(UTC)
            approval.resume_state = {**(approval.resume_state or {}), "response": answer, "resumed": True, "decision": "respond"}
            self._record_run_step(db, agent_run_id, "finalize", status="responded", output={"answer_preview": answer[:200]})
            self._finish_run(db, agent_run_id, status="responded", answer=answer, state={"approval_id": str(approval.id), "decision": "respond"})
            result = self._finish_resumed_turn(
                db=db,
                session_id=session_id,
                turn_id=turn_id,
                answer=answer,
                status="responded",
                approval=approval,
                tool_results=list(checkpoint.get("tool_results") or []),
            )
            return result
        if normalized_decision == "reject":
            answer = response or "Approval was rejected, so I stopped that tool call."
            approval.status = "rejected"
            approval.resolved_at = datetime.now(UTC)
            approval.resume_state = {**(approval.resume_state or {}), "resumed": True, "decision": "reject"}
            self._record_run_step(db, agent_run_id, "finalize", status="rejected", output={"answer_preview": answer[:200]})
            self._finish_run(db, agent_run_id, status="rejected", answer=answer, state={"approval_id": str(approval.id), "decision": "reject"})
            result = self._finish_resumed_turn(
                db=db,
                session_id=session_id,
                turn_id=turn_id,
                answer=answer,
                status="rejected",
                approval=approval,
                tool_results=list(checkpoint.get("tool_results") or []),
            )
            return result

        approval.status = "approved"
        approval.resolved_at = datetime.now(UTC)
        arguments = approval.edited_arguments if approval.edited_arguments else pending_tool_call.get("arguments")
        tool_name = str(pending_tool_call.get("name") or pending_tool_call.get("tool_name") or "")
        if self.tools is None:
            raise RuntimeError("tool executor is not configured")
        pending_tool_call = {**pending_tool_call, "arguments": arguments if isinstance(arguments, dict) else {}}
        round_tool_calls = list(checkpoint.get("round_tool_calls") or [pending_tool_call])
        pending_index = int(checkpoint.get("pending_index") or 0)
        round_completed_results = list(checkpoint.get("round_completed_results") or [])
        stored_tool_results = list(checkpoint.get("tool_results") or [])
        approval_marker_count = 1 if stored_tool_results and stored_tool_results[-1].get("status") == "approval_required" else 0
        prior_count = max(0, len(stored_tool_results) - len(round_completed_results) - approval_marker_count)
        prior_tool_results = stored_tool_results[:prior_count]
        round_results = list(round_completed_results)
        for index, call in enumerate(round_tool_calls[pending_index:], start=pending_index):
            active_call = pending_tool_call if index == pending_index else call
            active_tool_name = str(active_call.get("name") or active_call.get("tool_name") or "")
            active_arguments = active_call.get("arguments") if isinstance(active_call.get("arguments"), dict) else {}
            try:
                tool_result = self.tools.execute(
                    db,
                    tool_name=active_tool_name,
                    arguments=active_arguments,
                    turn_id=turn_id,
                    approved=True if index == pending_index else bool(active_call.get("approved", False)),
                )
            except ToolApprovalRequired as exc:
                approval_result = {
                    "status": "approval_required",
                    "error": str(exc),
                    "tool_name": active_tool_name,
                    "approval_id": exc.approval_id,
                }
                nested_checkpoint = {
                    **checkpoint,
                    "tool_results": [*prior_tool_results, *round_results, approval_result],
                    "round_completed_results": round_results,
                    "pending_tool_call": dict(active_call),
                    "pending_index": index,
                    "run_id": agent_run_id,
                    "thread_id": session_id,
                }
                self._save_approval_checkpoint(db, exc.approval_id, nested_checkpoint)
                self._save_graph_checkpoint(
                    thread_id=session_id,
                    run_id=agent_run_id,
                    checkpoint_id=f"{turn_id}:approval:{exc.approval_id}",
                    state=nested_checkpoint,
                )
                self._record_run_step(
                    db,
                    agent_run_id,
                    "approval_or_continue",
                    status="approval_required",
                    output={"approval_id": exc.approval_id, "pending_tool_call": dict(active_call)},
                )
                return AgentTurnResult(
                    turn_id=turn_id,
                    answer="Approval is required before I can continue this turn.",
                    context={
                        "tools": nested_checkpoint["tool_results"],
                        "turn": {
                            "status": "approval_required",
                            "approval_id": exc.approval_id,
                            "pending_tool_call": dict(active_call),
                            "resume_available": True,
                        },
                    },
                    status="approval_required",
                    approval_id=exc.approval_id,
                    pending_tool_call=dict(active_call),
                    resume_available=True,
                )
            round_results.append(tool_result)
            if self.conversation is not None:
                self.conversation.record_tool_message(
                    db,
                    session_id=session_id,
                    turn_id=turn_id,
                    content=json.dumps(tool_result, ensure_ascii=False, default=str)[:4000],
                    metadata={"tool_name": active_tool_name, "approval_id": str(approval.id), "resumed": True},
                )
        round_tool_calls[pending_index] = pending_tool_call
        if self.conversation is not None:
            self.events.emit("agent.node.tool_execution", {"tool_results": len(round_results)}, session_id=session_id, turn_id=turn_id)
        messages = self._append_tool_messages(
            list(checkpoint.get("messages") or []),
            round_tool_calls,
            round_results,
        )
        tool_results = [*prior_tool_results, *round_results]
        all_calls = list(checkpoint.get("tool_calls") or [])
        if self.llm is not None and self.llm.configured and messages:
            try:
                followup = self.llm.complete(messages=messages, tools=self.registry.openai_tools() if self.registry is not None else None)
                answer, all_calls, llm_status, messages, tool_results = self._continue_llm_loop(
                    db,
                    response=followup,
                    messages=messages,
                    tools=self.registry.openai_tools() if self.registry is not None else None,
                    all_calls=all_calls,
                    tool_results=tool_results,
                    turn_id=turn_id,
                    session_id=session_id,
                    max_rounds=4,
                    start_round=int(checkpoint.get("round_index") or 0) + 1,
                )
            except AgentApprovalInterrupt as interrupt:
                nested_checkpoint = {
                    **checkpoint,
                    "messages": interrupt.messages,
                    "tool_calls": interrupt.tool_calls,
                    "tool_results": interrupt.tool_results,
                    "round_tool_calls": interrupt.round_tool_calls,
                    "round_completed_results": interrupt.completed_results,
                    "pending_tool_call": interrupt.pending_tool_call,
                    "pending_index": interrupt.pending_index,
                    "round_index": interrupt.round_index,
                    "run_id": agent_run_id,
                    "thread_id": session_id,
                }
                self._save_approval_checkpoint(db, interrupt.approval_id, nested_checkpoint)
                self._save_graph_checkpoint(
                    thread_id=session_id,
                    run_id=agent_run_id,
                    checkpoint_id=f"{turn_id}:approval:{interrupt.approval_id}",
                    state=nested_checkpoint,
                )
                self._record_run_step(
                    db,
                    agent_run_id,
                    "approval_or_continue",
                    status="approval_required",
                    output={"approval_id": interrupt.approval_id, "pending_tool_call": interrupt.pending_tool_call},
                )
                return AgentTurnResult(
                    turn_id=turn_id,
                    answer="Approval is required before I can continue this turn.",
                    context={
                        "tools": interrupt.tool_results,
                        "turn": {
                            "status": "approval_required",
                            "approval_id": interrupt.approval_id,
                            "pending_tool_call": interrupt.pending_tool_call,
                            "resume_available": True,
                        },
                    },
                    status="approval_required",
                    approval_id=interrupt.approval_id,
                    pending_tool_call=interrupt.pending_tool_call,
                    resume_available=True,
                )
            except Exception as exc:  # noqa: BLE001
                self.events.emit("llm.resume_turn.failed", {"error": str(exc)}, severity="warning", session_id=session_id, turn_id=turn_id)
                answer = self._fallback_answer([], {"resident": [], "dynamic": []}, tool_results)
                llm_status = "failed"
        else:
            answer = self._fallback_answer([], {"resident": [], "dynamic": []}, tool_results)
            llm_status = "not_configured"
        approval.resume_state = {**(approval.resume_state or {}), "resumed": True, "decision": normalized_decision}
        curator_summary = self._curate_turn(
            db,
            session_id=session_id,
            turn_id=turn_id,
            message=str(checkpoint.get("message") or ""),
            answer=answer,
            tool_results=tool_results,
            a2a_result={},
        )
        context = {
            "tools": tool_results,
            "llm": {"status": llm_status, "tool_calls": all_calls},
            "memory_curator": curator_summary,
            "turn": {"status": "completed", "approval_id": str(approval.id), "resume_available": False},
            "run": {"run_id": agent_run_id, "thread_id": session_id},
        }
        self.events.emit("turn.resumed", {"approval_id": str(approval.id), "tool_name": tool_name}, session_id=session_id, turn_id=turn_id)
        self.events.emit("agent.node.final_answer", {"llm_status": llm_status, "tool_results": len(tool_results)}, session_id=session_id, turn_id=turn_id)
        self._record_run_step(
            db,
            agent_run_id,
            "tools",
            output={"resumed": True, "results": len(tool_results)},
        )
        self._record_run_step(
            db,
            agent_run_id,
            "finalize",
            output={"answer_preview": answer[:200], "llm_status": llm_status},
        )
        if self.conversation is not None:
            self.conversation.record_assistant_message(
                db,
                session_id=session_id,
                turn_id=turn_id,
                content=answer,
                metadata={"llm_status": llm_status, "resumed_from_approval": str(approval.id)},
            )
            self.conversation.update_summary_if_needed(db, session_id=session_id, llm=self.llm)
        self._record_run_step(db, agent_run_id, "post_turn", output={"event_type": "turn.resumed", "curator": curator_summary})
        self._finish_run(db, agent_run_id, status="completed", answer=answer, state={"approval_id": str(approval.id), "decision": normalized_decision})
        return AgentTurnResult(turn_id=turn_id, answer=answer, context=context, status="completed")

    def _finish_resumed_turn(
        self,
        *,
        db,
        session_id: str,
        turn_id: str,
        answer: str,
        status: str,
        approval: Approval,
        tool_results: list[dict[str, Any]],
    ) -> AgentTurnResult:
        context = {
            "tools": tool_results,
            "turn": {
                "status": status,
                "approval_id": str(approval.id),
                "resume_available": False,
            },
        }
        self.events.emit("turn.resumed", {"approval_id": str(approval.id), "status": status}, session_id=session_id, turn_id=turn_id)
        if self.conversation is not None:
            self.conversation.record_assistant_message(
                db,
                session_id=session_id,
                turn_id=turn_id,
                content=answer,
                metadata={"llm_status": status, "resumed_from_approval": str(approval.id)},
            )
        return AgentTurnResult(
            turn_id=turn_id,
            answer=answer,
            context=context,
            status=status,
            approval_id=str(approval.id),
            resume_available=False,
        )

    def _save_approval_checkpoint(self, db, approval_id: str, checkpoint: dict[str, Any]) -> None:
        if not approval_id or db is None:
            return
        try:
            approval = db.get(Approval, uuid.UUID(str(approval_id)))
        except (TypeError, ValueError):
            return
        if approval is None:
            return
        pending_tool_call = checkpoint.get("pending_tool_call") if isinstance(checkpoint.get("pending_tool_call"), dict) else {}
        approval.turn_checkpoint = checkpoint
        approval.original_tool_call = pending_tool_call
        approval.allowed_decisions = ["approve", "edit", "reject", "respond"]
        approval.resume_state = {
            **(approval.resume_state or {}),
            "mode": "resume_turn",
            "run_id": checkpoint.get("run_id") or (approval.resume_state or {}).get("run_id") or "",
            "thread_id": checkpoint.get("thread_id") or checkpoint.get("session_id") or "",
            "pending_tool_call": pending_tool_call,
        }
        if approval.payload:
            approval.payload = {**approval.payload, "resume_mode": "agent_turn"}

    def _curate_turn(
        self,
        db,
        *,
        session_id: str,
        turn_id: str,
        message: str,
        answer: str,
        tool_results: list[dict[str, Any]],
        a2a_result: dict[str, Any],
    ) -> dict[str, Any]:
        if self.memory_curator is None:
            return {"created": 0, "proposal_ids": []}
        try:
            result = self.memory_curator.curate_turn(
                db,
                session_id=session_id,
                turn_id=turn_id,
                user_message=message,
                assistant_answer=answer,
                tool_results=tool_results,
                a2a_result=a2a_result,
            )
        except Exception as exc:  # noqa: BLE001
            self.events.emit(
                "memory.curator.failed",
                {"error": str(exc), "tool_results": len(tool_results)},
                severity="warning",
                session_id=session_id,
                turn_id=turn_id,
            )
            return {"created": 0, "proposal_ids": [], "error": str(exc)}
        return {"created": result.created, "proposal_ids": [str(item.id) for item in result.proposals]}

    def _run_llm(
        self,
        db,
        turn_id: str,
        session_id: str,
        message: str,
        wiki_orientation: dict[str, Any],
        memory_context: dict[str, Any],
        skill_index: list[dict[str, Any]],
        session_context: SessionContext,
        core_context: dict[str, Any] | None = None,
        task_plan: AgentTaskPlan | None = None,
    ) -> tuple[str, list[dict[str, Any]], str, list[dict[str, Any]], list[dict[str, Any]]]:
        if self.llm is None or not self.llm.configured:
            return "", [], "not_configured", [], []
        tools = self.registry.openai_tools() if self.registry is not None else None
        messages = [
            {
                "role": "system",
                "content": (
                    "You are SoulClaw. Memory stores durable facts and preferences; "
                    "Skills store procedural know-how; LLM-Wiki stores traceable evidence. "
                    "Follow the compact turn plan in context; keep the local assistant path as the default. "
                    "Use DeepResearch/A2A only when the plan says the task is research-grade or the user explicitly asks for it. "
                    "Use skill_search before reading a full skill, and skill_read only when the skill is relevant. "
                    "For Wiki-backed facts, first call wiki_route. If it recommends browse_first, use wiki_browse; "
                    "if search_first, use wiki_search; if bridge, combine wiki_search/wiki_browse with wiki_follow_links. "
                    "Always call wiki_read before relying on a page body, and use wiki_sufficiency_check for complex or constrained claims. "
                    "For durable memory facts, use memory_search and memory_get before relying on them. "
                    "memory_create and skill_use_trace create reviewable proposals; do not assume a proposal is active memory until applied. "
                    "Cite Wiki pages as [[page_key]] when using Wiki evidence. "
                    "Propose memory only for durable, user-useful lessons with evidence."
                ),
            },
            {
                "role": "system",
                "content": self._context_text(
                    wiki_orientation,
                    memory_context,
                    skill_index,
                    session_context,
                    core_context,
                    task_plan,
                ),
            },
            {"role": "user", "content": message},
        ]
        all_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        max_rounds = 4
        try:
            response = self.llm.complete(messages=messages, tools=tools)
        except RateLimitExceeded as exc:
            self.events.emit("llm.rate_limited", {"policy": exc.policy, "retry_after": exc.retry_after}, severity="warning")
            return "", [], "rate_limited", messages, []
        except CircuitOpenError as exc:
            self.events.emit("llm.circuit_open", {"error": str(exc)}, severity="warning")
            return "", [], "circuit_open", messages, []
        except Exception as exc:  # noqa: BLE001
            self.events.emit("llm.failed", {"error": str(exc)}, severity="warning")
            return "", [], "failed", messages, []
        return self._continue_llm_loop(
            db,
            response=response,
            messages=messages,
            tools=tools,
            all_calls=all_calls,
            tool_results=tool_results,
            turn_id=turn_id,
            session_id=session_id,
            max_rounds=max_rounds,
            start_round=0,
        )

    def _continue_llm_loop(
        self,
        db,
        *,
        response,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        all_calls: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        turn_id: str,
        session_id: str,
        max_rounds: int = 4,
        start_round: int = 0,
    ) -> tuple[str, list[dict[str, Any]], str, list[dict[str, Any]], list[dict[str, Any]]]:
        status = "completed_with_tools" if tool_results else "completed"
        for round_index in range(start_round, max_rounds):
            calls = [{"id": call.id, "name": call.name, "arguments": call.arguments} for call in response.tool_calls]
            if not calls:
                self.events.emit("llm.completed", {"tool_calls": [call["name"] for call in all_calls]})
                return response.content, all_calls, status, messages, tool_results
            all_calls.extend(calls)
            try:
                round_results = self._execute_tool_calls(db, calls, turn_id=turn_id, session_id=session_id)
            except AgentApprovalInterrupt as interrupt:
                interrupt.messages = messages
                interrupt.tool_calls = list(all_calls)
                interrupt.tool_results = [*tool_results, *interrupt.completed_results, interrupt.approval_result]
                interrupt.round_tool_calls = calls
                interrupt.round_index = round_index
                raise
            tool_results.extend(round_results)
            messages = self._append_tool_messages(messages, calls, round_results)
            try:
                response = self.llm.complete(messages=messages, tools=tools)
            except RateLimitExceeded as exc:
                self.events.emit("llm.tool_loop.rate_limited", {"policy": exc.policy, "retry_after": exc.retry_after}, severity="warning")
                return "", all_calls, "rate_limited", messages, tool_results
            except CircuitOpenError as exc:
                self.events.emit("llm.tool_loop.circuit_open", {"error": str(exc)}, severity="warning")
                return "", all_calls, "circuit_open", messages, tool_results
            except Exception as exc:  # noqa: BLE001
                self.events.emit("llm.tool_loop.failed", {"error": str(exc)}, severity="warning")
                return "", all_calls, "failed", messages, tool_results
            status = "completed_with_tools"
        self.events.emit("llm.tool_loop.max_rounds", {"rounds": max_rounds}, severity="warning")
        return response.content, all_calls, "max_tool_rounds", messages, tool_results

    def _execute_tool_calls(
        self,
        db,
        calls: list[dict[str, Any]],
        *,
        turn_id: str,
        session_id: str,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        if self.tools is None:
            return results
        for index, call in enumerate(calls):
            try:
                result = self.tools.execute(
                    db,
                    tool_name=str(call.get("name") or call.get("tool_name") or ""),
                    arguments=call.get("arguments") if isinstance(call.get("arguments"), dict) else {},
                    turn_id=turn_id,
                    approved=bool(call.get("approved", False)),
                )
            except ToolApprovalRequired as exc:
                result = {
                    "status": "approval_required",
                    "error": str(exc),
                    "tool_name": call.get("name") or call.get("tool_name"),
                    "approval_id": exc.approval_id,
                }
                if self.conversation is not None:
                    self.conversation.record_tool_message(
                        db,
                        session_id=session_id,
                        turn_id=turn_id,
                        content=json.dumps(result, ensure_ascii=False, default=str)[:4000],
                        metadata={"tool_name": call.get("name") or call.get("tool_name"), "approval_id": exc.approval_id},
                    )
                raise AgentApprovalInterrupt(
                    approval_id=exc.approval_id,
                    pending_tool_call=dict(call),
                    completed_results=results,
                    approval_result=result,
                    pending_index=index,
                ) from exc
            except PermissionError as exc:
                result = {"status": "approval_required", "error": str(exc), "tool_name": call.get("name") or call.get("tool_name")}
            except Exception as exc:  # noqa: BLE001
                result = {"status": "failed", "error": str(exc), "tool_name": call.get("name") or call.get("tool_name")}
            results.append(result)
            if self.conversation is not None:
                self.conversation.record_tool_message(
                    db,
                    session_id=session_id,
                    turn_id=turn_id,
                    content=json.dumps(result, ensure_ascii=False, default=str)[:4000],
                    metadata={"tool_name": call.get("name") or call.get("tool_name")},
                )
        return results

    @staticmethod
    def _append_tool_messages(
        messages: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        assistant_tool_calls = []
        for index, call in enumerate(tool_calls):
            tool_call_id = str(call.get("id") or f"tool_call_{index}")
            call["id"] = tool_call_id
            assistant_tool_calls.append(
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": str(call.get("name") or call.get("tool_name") or ""),
                        "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                    },
                }
            )
        return [
            *messages,
            {"role": "assistant", "content": "", "tool_calls": assistant_tool_calls},
            *[
                {
                    "role": "tool",
                    "tool_call_id": str(tool_calls[index].get("id") or f"tool_call_{index}"),
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
                for index, result in enumerate(tool_results)
            ],
        ]

    def _synthesize_after_tools(
        self,
        messages: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
    ) -> str:
        if self.llm is None or not self.llm.configured or not messages:
            return ""
        assistant_tool_calls = []
        for index, call in enumerate(tool_calls):
            tool_call_id = str(call.get("id") or f"tool_call_{index}")
            call["id"] = tool_call_id
            assistant_tool_calls.append(
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": str(call.get("name") or call.get("tool_name") or ""),
                        "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                    },
                }
            )
        followup_messages = [
            *messages,
            {"role": "assistant", "content": "", "tool_calls": assistant_tool_calls},
            *[
                {
                    "role": "tool",
                    "tool_call_id": str(tool_calls[index].get("id") or f"tool_call_{index}"),
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
                for index, result in enumerate(tool_results)
            ],
        ]
        try:
            response = self.llm.complete(messages=followup_messages, tools=None)
        except Exception as exc:  # noqa: BLE001
            self.events.emit("llm.tool_synthesis.failed", {"error": str(exc)}, severity="warning")
            return ""
        self.events.emit("llm.tool_synthesis.completed", {"tool_results": len(tool_results)})
        return response.content

    @staticmethod
    def _context_text(
        wiki_orientation: dict[str, Any],
        memory_context: dict[str, Any],
        skill_index: list[dict[str, Any]] | None = None,
        session_context: SessionContext | None = None,
        core_context: dict[str, Any] | None = None,
        task_plan: AgentTaskPlan | None = None,
    ) -> str:
        session_context = session_context or SessionContext(summary="", messages=[], total_messages=0)
        task_plan = task_plan or AgentRuntime._plan_turn("")
        conversation_lines = [f"- {item.role}: {item.content[:800]}" for item in session_context.messages]
        wiki_lines = [
            f"- {item.get('page_key')}: {item.get('title')} :: {item.get('summary')}"
            for item in wiki_orientation.get("pages", [])[:40]
        ]
        resident = memory_context.get("resident", [])
        dynamic = memory_context.get("dynamic", [])
        memory_lines = [AgentRuntime._memory_context_line(item) for item in [*resident, *dynamic]]
        skill_lines = [
            f"- {item.get('skill_key')}: {item.get('name')} :: {item.get('description', '')}"
            for item in (skill_index or [])[:20]
        ]
        core_context = core_context or {}
        soul = core_context.get("soul")
        user = core_context.get("user")
        soul_text = getattr(soul, "content", "")[:2500] if soul is not None else ""
        user_text = getattr(user, "content", "")[:2500] if user is not None else ""
        return (
            "Turn plan:\n"
            + json.dumps(task_plan.to_context(), ensure_ascii=False)
            + "\n\n"
            "Soul core block (authoritative structured state):\n"
            + (soul_text or "(none)")
            + "\n\nUser core block (authoritative structured state):\n"
            + (user_text or "(none)")
            + "\n\n"
            "Conversation summary:\n"
            + (session_context.summary or "(none)")
            + "\n\nRecent conversation:\n"
            + "\n".join(conversation_lines)
            + "\n\nMemory context:\n"
            + "\n".join(memory_lines)
            + "\n\nSkill index (search/read full skills only when relevant):\n"
            + ("\n".join(skill_lines) or "(none)")
            + "\n\nLLM-Wiki orientation:\n"
            + "Schema present: "
            + str(bool(wiki_orientation.get("schema")))
            + "\nIndex excerpt:\n"
            + str(wiki_orientation.get("index", ""))[:2000]
            + "\nRecent log:\n"
            + str(wiki_orientation.get("recent_log", ""))[:1200]
            + "\nPage index map:\n"
            + "\n".join(wiki_lines)
        )

    @staticmethod
    def _memory_context_line(item: Any) -> str:
        confidence = float(getattr(item, "confidence", 0.0) or 0.0)
        stability = float(getattr(item, "stability", 0.0) or 0.0)
        source = str(getattr(item, "source", "") or "")
        content = str(getattr(item, "content", "") or "")
        return (
            f"- id={getattr(item, 'id', '')} kind={getattr(item, 'kind', '')} source={source} "
            f"confidence={confidence:.2f} stability={stability:.2f}: {content}"
        )

    def _skill_index(self, db, message: str) -> list[dict[str, Any]]:
        if self.registry is None or not hasattr(self.registry, "skills"):
            return []
        try:
            hits = self.registry.skills.search(db, message, limit=12)
        except Exception as exc:  # noqa: BLE001
            self.events.emit("skills.context.failed", {"error": str(exc)}, severity="warning")
            return []
        return [
            {
                "skill_key": item["skill"].skill_key,
                "name": item["skill"].name,
                "description": item["skill"].description,
                "score": item.get("score", 0.0),
            }
            for item in hits
        ]

    def _record_run_step(
        self,
        db,
        run_id: str,
        step_name: str,
        *,
        status: str = "succeeded",
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        if self.runs is None or not run_id:
            return
        try:
            self.runs.add_step(db, run_id, step_name, status=status, input=input, output=output, error=error)
        except Exception as exc:  # noqa: BLE001
            self.events.emit("agent.run.step_record_failed", {"run_id": run_id, "step": step_name, "error": str(exc)}, severity="warning")

    def _finish_run(
        self,
        db,
        run_id: str,
        *,
        status: str,
        answer: str = "",
        error: str = "",
        state: dict[str, Any] | None = None,
    ) -> None:
        if self.runs is None or not run_id:
            return
        try:
            self.runs.finish_run(db, run_id, status=status, answer=answer, error=error, state=state)
        except Exception as exc:  # noqa: BLE001
            self.events.emit("agent.run.finish_failed", {"run_id": run_id, "error": str(exc)}, severity="warning")

    def _save_graph_checkpoint(self, *, thread_id: str, run_id: str, checkpoint_id: str, state: dict[str, Any]) -> None:
        if self.checkpoints is None or not run_id:
            return
        self.checkpoints.save(thread_id=thread_id, run_id=run_id, checkpoint_id=checkpoint_id, state=state)

    def _approval_context(
        self,
        *,
        session_id: str,
        agent_run_id: str,
        task_plan: AgentTaskPlan,
        approval_id: str,
        pending_tool_call: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "wiki": [],
            "memory": {"resident": [], "dynamic": []},
            "conversation": {"summary": False, "recent_messages": 0, "total_messages": 0},
            "wiki_orientation": {"page_count": 0, "canonical_files": False},
            "workspace": {},
            "skills": {"indexed": 0, "items": []},
            "tools": [
                {
                    "status": "approval_required",
                    "tool_name": pending_tool_call.get("name") or pending_tool_call.get("tool_name") or "",
                    "approval_id": approval_id,
                }
            ],
            "a2a": {},
            "task_plan": task_plan.to_context(),
            "llm": {"status": "approval_required", "tool_calls": [pending_tool_call], "wiki_read_used": False},
            "trace": {"trace_id": current_trace_id()},
            "turn": {
                "status": "approval_required",
                "approval_id": approval_id,
                "pending_tool_call": pending_tool_call,
                "resume_available": True,
            },
            "run": {
                "run_id": agent_run_id,
                "thread_id": session_id,
                "graph": self.graph_shell.state(),
            },
        }

    @staticmethod
    def _plan_turn(message: str, *, tool_calls: list[dict[str, Any]] | None = None) -> AgentTaskPlan:
        if tool_calls is not None:
            return AgentTaskPlan(
                route="tool_execution",
                reason="Explicit tool calls were supplied by the caller.",
                memory_policy="Do not infer new long-term memory from raw tool execution alone.",
                wiki_policy="Use Wiki only if the supplied tools request it.",
                skill_policy="Use existing skills only as explicit procedural guidance.",
            )
        if AgentRuntime._should_delegate_to_deep_research(message):
            return AgentTaskPlan(
                route="deep_research",
                reason="The user explicitly asked for broad, current, or evidence-heavy research.",
                memory_policy=(
                    "After delegated research completes, save only durable user preferences, repeated procedures, "
                    "or evidence-backed lessons as proposals."
                ),
                wiki_policy="Treat external research findings as Wiki candidates with citations, not as raw memory.",
                skill_policy="Promote a research procedure to Skill only after repeated successful runs.",
                delegation={
                    "capability": "deep-research",
                    "options": {
                        "retrieval_policy": {
                            "require_current_sources": True,
                            "return_artifacts": True,
                            "preserve_evidence": True,
                        }
                    },
                },
            )
        return AgentTaskPlan(
            route="local_answer",
            reason="Default local-first turn: answer with resident memory, Wiki evidence, and tools only as needed.",
            memory_policy="Use resident and dynamic memory as personalization; create memory only for durable facts or preferences.",
            wiki_policy="Use Wiki for traceable facts; read page bodies before relying on page evidence.",
            skill_policy="Search/read skills only when a procedural pattern is clearly relevant.",
        )

    @staticmethod
    def _should_delegate_to_deep_research(message: str) -> bool:
        text = str(message or "").lower()
        suppressors = (
            "local only",
            "no deep research",
            "do not delegate",
            "don't delegate",
            "不要委托",
            "不要调用",
            "不要联网",
            "本地回答",
            "不需要联网",
        )
        if any(item in text for item in suppressors):
            return False
        triggers = (
            "deepresearch",
            "deep research",
            "deep-research",
            "深度研究",
            "深入调研",
            "全面调研",
            "广泛调研",
            "联网调研",
            "研究报告",
            "调研报告",
            "竞品调研",
            "research report",
            "evidence report",
        )
        return any(trigger in text for trigger in triggers)

    @staticmethod
    def _delegated_answer(result: dict[str, Any]) -> str:
        if result.get("status") == "failed":
            return f"A2A delegation failed: {result.get('error') or 'unknown error'}"
        task_id = result.get("task_id") or ""
        status = result.get("status") or ""
        answer = result.get("answer") or ""
        artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), list) else []
        parts = ["A2A delegation completed" if status == "completed" else "A2A delegation started"]
        if task_id:
            parts.append(f"task_id={task_id}")
        if status:
            parts.append(f"status={status}")
        if artifacts:
            parts.append(f"artifacts={len(artifacts)}")
        if answer:
            parts.append("\n\n" + str(answer))
        return " ".join(parts)

    @staticmethod
    def _fallback_answer(
        wiki_hits: list[dict[str, Any]],
        memory_context: dict[str, Any],
        tool_results: list[dict[str, Any]],
    ) -> str:
        parts = ["SoulClaw runtime is online."]
        if wiki_hits:
            parts.append(f"Retrieved {len(wiki_hits)} Wiki item(s).")
        dynamic_memory = memory_context.get("dynamic", [])
        resident_memory = memory_context.get("resident", [])
        if resident_memory or dynamic_memory:
            parts.append(f"Retrieved {len(resident_memory)} resident and {len(dynamic_memory)} dynamic memory item(s).")
        if tool_results:
            parts.append(f"Executed {len(tool_results)} tool call(s).")
        return " ".join(parts)

    @staticmethod
    def _degraded_answer(llm_status: str) -> str:
        messages = {
            "failed": "The model call failed. The turn was recorded with trace context for troubleshooting.",
            "rate_limited": "The model call was rate limited. Please retry after the configured cooldown window.",
            "circuit_open": "The model circuit breaker is open. Please retry after the recovery window.",
        }
        return messages.get(llm_status, "")
