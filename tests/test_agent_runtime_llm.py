from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.runtime.agent import AgentRuntime
from backend.runtime.llm import LLMResponse, LLMToolCall, OpenAICompatibleClient


class DummyEvents:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event_type, payload=None, **kwargs) -> None:
        self.events.append((event_type, payload or {}, kwargs))


class DummyPage:
    page_key = "index"
    title = "Index"
    summary = "Summary"


class DummyWiki:
    def search(self, db, message, limit=5):
        return [{"page": DummyPage(), "source": "postgres", "score": 1.0}]


class DummyMemory:
    def resident_context(self, db, message, dynamic_limit=5):
        return {"resident": [], "dynamic": []}


@dataclass
class DummyToolExecutor:
    calls: list[dict[str, Any]]

    def execute(self, db, *, tool_name: str, arguments: dict[str, Any] | None = None, turn_id: str = "", approved: bool = False):
        self.calls.append({"tool_name": tool_name, "arguments": arguments or {}, "turn_id": turn_id, "approved": approved})
        return {"tool_name": tool_name, "status": "succeeded", "result": {"items": [{"title": "Tool Hit"}]}}


class DummyRegistry:
    def openai_tools(self):
        return [{"type": "function", "function": {"name": "wiki_search", "parameters": {"type": "object"}}}]


class TwoStepLLM:
    configured = True

    def __init__(self) -> None:
        self.messages: list[list[dict[str, Any]]] = []

    def complete(self, *, messages, tools=None, temperature=0.2):
        del temperature
        self.messages.append(messages)
        if tools:
            return LLMResponse(
                content="",
                tool_calls=[LLMToolCall(name="wiki_search", arguments={"query": "abc"}, id="call_1")],
                raw={},
            )
        return LLMResponse(content="final answer from tool result", tool_calls=[], raw={})


def test_openai_tool_call_parser_handles_json_arguments() -> None:
    calls = OpenAICompatibleClient._parse_tool_calls(
        [{"function": {"name": "wiki_search", "arguments": "{\"query\":\"abc\"}"}}]
    )

    assert calls[0].name == "wiki_search"
    assert calls[0].arguments == {"query": "abc"}


def test_agent_runtime_fallback_uses_retrieved_context() -> None:
    events = DummyEvents()
    runtime = AgentRuntime(wiki=DummyWiki(), memory=DummyMemory(), events=events)

    result = runtime.run_turn(None, "hello")

    assert "Retrieved 1 Wiki item" in result.answer
    assert result.context["llm"]["status"] == "not_configured"


def test_agent_runtime_synthesizes_answer_after_llm_tool_call() -> None:
    events = DummyEvents()
    tool_executor = DummyToolExecutor(calls=[])
    llm = TwoStepLLM()
    runtime = AgentRuntime(
        wiki=DummyWiki(),
        memory=DummyMemory(),
        events=events,
        tools=tool_executor,
        registry=DummyRegistry(),
        llm=llm,
    )

    result = runtime.run_turn(None, "use a tool")

    assert result.answer == "final answer from tool result"
    assert result.context["llm"]["status"] == "completed_with_tools"
    assert tool_executor.calls[0]["tool_name"] == "wiki_search"
    assert llm.messages[1][-1]["role"] == "tool"
    assert llm.messages[1][-1]["tool_call_id"] == "call_1"
