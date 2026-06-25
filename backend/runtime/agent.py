"""Minimal agent turn pipeline."""

import json
import uuid
from dataclasses import dataclass
from typing import Any

from backend.domain.memory import MemoryService
from backend.domain.tools import ToolExecutor, ToolRegistry
from backend.domain.wiki import WikiService
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
    ) -> None:
        self.wiki = wiki
        self.memory = memory
        self.events = events
        self.tools = tools
        self.registry = registry
        self.llm = llm

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
        wiki_hits = self.wiki.search(db, message, limit=5)
        memory_context = self.memory.resident_context(db, message, dynamic_limit=5)
        tool_results: list[dict[str, Any]] = []
        inferred_tool_calls: list[dict[str, Any]] = []
        llm_answer = ""
        llm_status = "not_configured"
        llm_messages: list[dict[str, Any]] = []
        if tool_calls is None:
            llm_answer, inferred_tool_calls, llm_status, llm_messages = self._run_llm(
                message,
                wiki_hits,
                memory_context,
            )
        if self.tools is not None:
            for call in tool_calls if tool_calls is not None else inferred_tool_calls:
                try:
                    tool_results.append(
                        self.tools.execute(
                            db,
                            tool_name=str(call.get("name") or call.get("tool_name") or ""),
                            arguments=call.get("arguments") if isinstance(call.get("arguments"), dict) else {},
                            turn_id=turn_id,
                            approved=bool(call.get("approved", False)),
                        )
                    )
                except PermissionError as exc:
                    tool_results.append({"status": "approval_required", "error": str(exc), "tool_name": call.get("name") or call.get("tool_name")})
                except Exception as exc:  # noqa: BLE001
                    tool_results.append({"status": "failed", "error": str(exc), "tool_name": call.get("name") or call.get("tool_name")})
        if (
            tool_calls is None
            and inferred_tool_calls
            and len(tool_results) == len(inferred_tool_calls)
            and llm_status == "completed"
        ):
            synthesized = self._synthesize_after_tools(llm_messages, inferred_tool_calls, tool_results)
            if synthesized:
                llm_answer = synthesized
                llm_status = "completed_with_tools"
        answer = llm_answer or self._fallback_answer(wiki_hits, memory_context, tool_results)
        context = {
            "wiki": [
                {
                    "page_key": item["page"].page_key,
                    "title": item["page"].title,
                    "source": item["source"],
                    "score": item["score"],
                }
                for item in wiki_hits
            ],
            "memory": {
                "resident": [str(item.id) for item in memory_context["resident"]],
                "dynamic": [str(item.id) for item in memory_context["dynamic"]],
            },
            "tools": tool_results,
            "llm": {"status": llm_status, "tool_calls": inferred_tool_calls},
        }
        self.events.emit("turn.completed", {"context": context}, session_id=session_id, turn_id=turn_id)
        return AgentTurnResult(turn_id=turn_id, answer=answer, context=context)

    def _run_llm(
        self,
        message: str,
        wiki_hits: list[dict[str, Any]],
        memory_context: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]], str, list[dict[str, Any]]]:
        if self.llm is None or not self.llm.configured:
            return "", [], "not_configured", []
        tools = self.registry.openai_tools() if self.registry is not None else None
        messages = [
            {
                "role": "system",
                "content": (
                    "You are ZLAgent. Use retrieved Wiki and Memory context. "
                    "Call tools only when they materially improve the answer."
                ),
            },
            {"role": "system", "content": self._context_text(wiki_hits, memory_context)},
            {"role": "user", "content": message},
        ]
        try:
            response = self.llm.complete(messages=messages, tools=tools)
        except Exception as exc:  # noqa: BLE001
            self.events.emit("llm.failed", {"error": str(exc)}, severity="warning")
            return "", [], "failed", messages
        calls = [{"id": call.id, "name": call.name, "arguments": call.arguments} for call in response.tool_calls]
        self.events.emit("llm.completed", {"tool_calls": [call["name"] for call in calls]})
        return response.content, calls, "completed", messages

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
    def _context_text(wiki_hits: list[dict[str, Any]], memory_context: dict[str, Any]) -> str:
        wiki_lines = [
            f"- {item['page'].page_key}: {item['page'].title} :: {item['page'].summary}"
            for item in wiki_hits
        ]
        resident = memory_context.get("resident", [])
        dynamic = memory_context.get("dynamic", [])
        memory_lines = [f"- {item.kind}: {item.content}" for item in [*resident, *dynamic]]
        return "Wiki context:\n" + "\n".join(wiki_lines) + "\n\nMemory context:\n" + "\n".join(memory_lines)

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
