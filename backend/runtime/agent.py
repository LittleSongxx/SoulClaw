"""Minimal agent turn pipeline."""

import json
import uuid
from dataclasses import dataclass
from typing import Any

from backend.domain.conversation import ConversationService, SessionContext
from backend.domain.memory import MemoryService
from backend.domain.tools import ToolExecutor, ToolRegistry
from backend.domain.wiki import WikiService
from backend.domain.workspace import WorkspaceService
from backend.infra.events import RuntimeEventBus
from backend.runtime.llm import OpenAICompatibleClient


@dataclass(frozen=True)
class AgentTurnResult:
    turn_id: str
    answer: str
    context: dict[str, Any]


class AgentRuntime:
    """Prepare -> retrieve context -> respond -> post-turn event.

    This is a deliberately small first pipeline. It gives the platform a clean
    place to attach LLM/tool-loop/approval behavior while already exercising
    wiki, memory, and event contracts.
    """

    def __init__(
        self,
        wiki: WikiService,
        memory: MemoryService,
        events: RuntimeEventBus,
        tools: ToolExecutor | None = None,
        registry: ToolRegistry | None = None,
        llm: OpenAICompatibleClient | None = None,
        conversation: ConversationService | None = None,
        workspace: WorkspaceService | None = None,
    ) -> None:
        self.wiki = wiki
        self.memory = memory
        self.events = events
        self.tools = tools
        self.registry = registry
        self.llm = llm
        self.conversation = conversation
        self.workspace = workspace

    def run_turn(
        self,
        db,
        message: str,
        *,
        session_id: str = "local",
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> AgentTurnResult:
        turn_id = str(uuid.uuid4())
        self.events.emit("turn.started", {"message_preview": message[:200]}, session_id=session_id, turn_id=turn_id)
        session_context = (
            self.conversation.recent_context(db, session_id, limit=12)
            if self.conversation is not None
            else SessionContext(summary="", messages=[], total_messages=0)
        )
        if self.conversation is not None:
            self.conversation.record_user_message(db, session_id=session_id, turn_id=turn_id, content=message)
        wiki_orientation = self.wiki.orientation(db) if hasattr(self.wiki, "orientation") else {"pages": [], "index": "", "schema": "", "recent_log": ""}
        wiki_hits = self.wiki.search(db, message, limit=5) if self.llm is None or not self.llm.configured else []
        memory_context = self.memory.resident_context(db, message, dynamic_limit=5)
        workspace_context = self.workspace.read_all() if self.workspace is not None else {}
        tool_results: list[dict[str, Any]] = []
        inferred_tool_calls: list[dict[str, Any]] = []
        llm_answer = ""
        llm_status = "not_configured"
        llm_messages: list[dict[str, Any]] = []
        if tool_calls is None:
            llm_answer, inferred_tool_calls, llm_status, llm_messages, tool_results = self._run_llm(
                db,
                turn_id,
                session_id,
                message,
                wiki_orientation,
                memory_context,
                session_context,
                workspace_context,
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
        answer = llm_answer or self._fallback_answer(wiki_hits, memory_context, tool_results)
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
            "workspace": {kind: {"path": item.path, "updated_at": item.updated_at} for kind, item in workspace_context.items()},
            "tools": tool_results,
            "llm": {"status": llm_status, "tool_calls": inferred_tool_calls, "wiki_read_used": wiki_read_used},
        }
        self.events.emit("turn.completed", {"context": context}, session_id=session_id, turn_id=turn_id)
        return AgentTurnResult(turn_id=turn_id, answer=answer, context=context)

    def _run_llm(
        self,
        db,
        turn_id: str,
        session_id: str,
        message: str,
        wiki_orientation: dict[str, Any],
        memory_context: dict[str, Any],
        session_context: SessionContext,
        workspace_context: dict[str, Any] | None = None,
    ) -> tuple[str, list[dict[str, Any]], str, list[dict[str, Any]], list[dict[str, Any]]]:
        if self.llm is None or not self.llm.configured:
            return "", [], "not_configured", [], []
        tools = self.registry.openai_tools() if self.registry is not None else None
        messages = [
            {
                "role": "system",
                "content": (
                    "You are ZLAgent. Use Memory context and LLM-Wiki tools. "
                    "For Wiki-backed facts, first orient/search, then call wiki_read before answering. "
                    "For durable memory facts, use memory_search and memory_get before relying on them. "
                    "Cite Wiki pages as [[page_key]] when using Wiki evidence."
                ),
            },
            {
                "role": "system",
                "content": self._context_text(wiki_orientation, memory_context, session_context, workspace_context),
            },
            {"role": "user", "content": message},
        ]
        all_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        status = "completed"
        max_rounds = 4
        try:
            response = self.llm.complete(messages=messages, tools=tools)
        except Exception as exc:  # noqa: BLE001
            self.events.emit("llm.failed", {"error": str(exc)}, severity="warning")
            return "", [], "failed", messages, []
        for _round in range(max_rounds):
            calls = [{"id": call.id, "name": call.name, "arguments": call.arguments} for call in response.tool_calls]
            if not calls:
                self.events.emit("llm.completed", {"tool_calls": [call["name"] for call in all_calls]})
                return response.content, all_calls, status, messages, tool_results
            all_calls.extend(calls)
            round_results = self._execute_tool_calls(db, calls, turn_id=turn_id, session_id=session_id)
            tool_results.extend(round_results)
            messages = self._append_tool_messages(messages, calls, round_results)
            try:
                response = self.llm.complete(messages=messages, tools=tools)
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
        for call in calls:
            try:
                result = self.tools.execute(
                    db,
                    tool_name=str(call.get("name") or call.get("tool_name") or ""),
                    arguments=call.get("arguments") if isinstance(call.get("arguments"), dict) else {},
                    turn_id=turn_id,
                    approved=bool(call.get("approved", False)),
                )
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
        session_context: SessionContext | None = None,
        workspace_context: dict[str, Any] | None = None,
    ) -> str:
        session_context = session_context or SessionContext(summary="", messages=[], total_messages=0)
        conversation_lines = [f"- {item.role}: {item.content[:800]}" for item in session_context.messages]
        wiki_lines = [
            f"- {item.get('page_key')}: {item.get('title')} :: {item.get('summary')}"
            for item in wiki_orientation.get("pages", [])[:40]
        ]
        resident = memory_context.get("resident", [])
        dynamic = memory_context.get("dynamic", [])
        memory_lines = [f"- {item.kind}: {item.content}" for item in [*resident, *dynamic]]
        workspace_context = workspace_context or {}
        soul = workspace_context.get("soul")
        user = workspace_context.get("user")
        memory_file = workspace_context.get("memory")
        soul_text = getattr(soul, "content", "")[:2500] if soul is not None else ""
        user_text = getattr(user, "content", "")[:2500] if user is not None else ""
        memory_text = getattr(memory_file, "content", "")[:3000] if memory_file is not None else ""
        return (
            "SOUL.md:\n"
            + (soul_text or "(none)")
            + "\n\nUSER.md:\n"
            + (user_text or "(none)")
            + "\n\nMEMORY.md excerpt:\n"
            + (memory_text or "(none)")
            + "\n\n"
            "Conversation summary:\n"
            + (session_context.summary or "(none)")
            + "\n\nRecent conversation:\n"
            + "\n".join(conversation_lines)
            + "\n\nMemory context:\n"
            + "\n".join(memory_lines)
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
    def _fallback_answer(
        wiki_hits: list[dict[str, Any]],
        memory_context: dict[str, Any],
        tool_results: list[dict[str, Any]],
    ) -> str:
        parts = ["ZLAgent runtime is online."]
        if wiki_hits:
            parts.append(f"Retrieved {len(wiki_hits)} Wiki item(s).")
        dynamic_memory = memory_context.get("dynamic", [])
        resident_memory = memory_context.get("resident", [])
        if resident_memory or dynamic_memory:
            parts.append(f"Retrieved {len(resident_memory)} resident and {len(dynamic_memory)} dynamic memory item(s).")
        if tool_results:
            parts.append(f"Executed {len(tool_results)} tool call(s).")
        return " ".join(parts)
