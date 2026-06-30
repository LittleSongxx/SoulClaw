from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from backend.domain.conversation import SessionContext
from backend.domain.tools import ToolApprovalRequired
from backend.infra.models import Approval
from backend.runtime.agent import AgentRuntime
from backend.infra.rate_limit import RateLimitExceeded
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
    path = "index.md"


class DummyMessage:
    role = "user"
    content = "previous context"


class DummyWiki:
    def orientation(self, db):
        del db
        return {"schema": "schema", "index": "[[index]] Snippet hit", "recent_log": "", "pages": [{"page_key": "index", "title": "Index", "summary": "Snippet hit"}], "page_count": 1}

    def search(self, db, message, limit=5):
        return [
            {
                "page": DummyPage(),
                "page_key": "index",
                "title": "Index",
                "path": "index.md",
                "summary": "Snippet hit",
                "source": "page_index",
                "score": 1.0,
            }
        ]


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
        if tools and len(self.messages) == 1:
            return LLMResponse(
                content="",
                tool_calls=[LLMToolCall(name="wiki_search", arguments={"query": "abc"}, id="call_1")],
                raw={},
            )
        return LLMResponse(content="final answer from tool result", tool_calls=[], raw={})


class WikiReadLLM:
    configured = True

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, messages, tools=None, temperature=0.2):
        del messages, tools, temperature
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(content="", tool_calls=[LLMToolCall(name="wiki_search", arguments={"query": "index"}, id="s1")], raw={})
        if self.calls == 2:
            return LLMResponse(content="", tool_calls=[LLMToolCall(name="wiki_read", arguments={"page_key": "index"}, id="r1")], raw={})
        return LLMResponse(content="Answer with [[index]]", tool_calls=[], raw={})


class ApprovalLLM:
    configured = True

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, messages, tools=None, temperature=0.2):
        del messages, tools, temperature
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(content="", tool_calls=[LLMToolCall(name="gateway_send", arguments={"text": "hello"}, id="send_1")], raw={})
        return LLMResponse(content="sent after approval", tool_calls=[], raw={})


class RateLimitedLLM:
    configured = True

    def complete(self, *, messages, tools=None, temperature=0.2):
        del messages, tools, temperature
        raise RateLimitExceeded(key="llm", policy="llm", limit=1, retry_after=30)


class ApprovalToolExecutor:
    def __init__(self, approval_id: uuid.UUID) -> None:
        self.approval_id = approval_id
        self.calls: list[dict[str, Any]] = []

    def execute(self, db, *, tool_name: str, arguments: dict[str, Any] | None = None, turn_id: str = "", approved: bool = False):
        del db
        self.calls.append({"tool_name": tool_name, "arguments": arguments or {}, "turn_id": turn_id, "approved": approved})
        if not approved:
            raise ToolApprovalRequired("tool requires approval: gateway_send", tool_name=tool_name, approval_id=str(self.approval_id))
        return {"tool_name": tool_name, "status": "succeeded", "result": {"sent": True, "arguments": arguments or {}}}


class ApprovalDB:
    def __init__(self, approval: Approval) -> None:
        self.approval = approval

    def get(self, model, item_id):
        if model is Approval and str(item_id) == str(self.approval.id):
            return self.approval
        return None


class DummyConversation:
    def __init__(self) -> None:
        self.recorded: list[tuple[str, str]] = []

    def recent_context(self, db, session_id: str, *, limit: int = 12):
        del db, limit
        return SessionContext(summary=f"summary for {session_id}", messages=[DummyMessage()], total_messages=1)

    def record_user_message(self, db, *, session_id: str, turn_id: str, content: str, metadata=None):
        del db, session_id, turn_id, metadata
        self.recorded.append(("user", content))

    def record_assistant_message(self, db, *, session_id: str, turn_id: str, content: str, metadata=None):
        del db, session_id, turn_id, metadata
        self.recorded.append(("assistant", content))

    def record_tool_message(self, db, *, session_id: str, turn_id: str, content: str, metadata=None):
        del db, session_id, turn_id, metadata
        self.recorded.append(("tool", content))

    def update_summary_if_needed(self, db, *, session_id: str, llm):
        del db, session_id, llm
        return None


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


def test_agent_runtime_records_session_messages_and_uses_context() -> None:
    events = DummyEvents()
    conversation = DummyConversation()
    llm = TwoStepLLM()
    runtime = AgentRuntime(
        wiki=DummyWiki(),
        memory=DummyMemory(),
        events=events,
        tools=DummyToolExecutor(calls=[]),
        registry=DummyRegistry(),
        llm=llm,
        conversation=conversation,
    )

    result = runtime.run_turn(None, "remember this", session_id="console")

    assert ("user", "remember this") in conversation.recorded
    assert ("assistant", "final answer from tool result") in conversation.recorded
    assert result.context["conversation"]["recent_messages"] == 1
    first_context_message = llm.messages[0][1]["content"]
    assert "summary for console" in first_context_message
    assert "previous context" in first_context_message
    assert "Snippet hit" in first_context_message


def test_agent_runtime_tool_loop_uses_wiki_read_before_answer() -> None:
    events = DummyEvents()
    tool_executor = DummyToolExecutor(calls=[])
    runtime = AgentRuntime(
        wiki=DummyWiki(),
        memory=DummyMemory(),
        events=events,
        tools=tool_executor,
        registry=DummyRegistry(),
        llm=WikiReadLLM(),
    )

    result = runtime.run_turn(None, "what is index")

    assert result.answer == "Answer with [[index]]"
    assert result.context["llm"]["wiki_read_used"] is True
    assert [call["tool_name"] for call in tool_executor.calls] == ["wiki_search", "wiki_read"]


def test_agent_runtime_reports_llm_rate_limit_as_degraded_state() -> None:
    runtime = AgentRuntime(
        wiki=DummyWiki(),
        memory=DummyMemory(),
        events=DummyEvents(),
        tools=DummyToolExecutor(calls=[]),
        registry=DummyRegistry(),
        llm=RateLimitedLLM(),
    )

    result = runtime.run_turn(None, "hello")

    assert result.context["llm"]["status"] == "rate_limited"
    assert "rate limited" in result.answer


def test_agent_runtime_interrupts_and_resumes_approval_checkpoint() -> None:
    approval_id = uuid.uuid4()
    approval = Approval(
        id=approval_id,
        status="pending",
        subject_type="tool_run",
        subject_id="gateway_send",
        payload={"tool_name": "gateway_send", "arguments": {"text": "hello"}},
        turn_checkpoint={},
        original_tool_call={},
        allowed_decisions=[],
        edited_arguments={},
        resume_state={},
    )
    db = ApprovalDB(approval)
    executor = ApprovalToolExecutor(approval_id)
    llm = ApprovalLLM()
    runtime = AgentRuntime(
        wiki=DummyWiki(),
        memory=DummyMemory(),
        events=DummyEvents(),
        tools=executor,
        registry=DummyRegistry(),
        llm=llm,
    )

    interrupted = runtime.run_turn(db, "send message", session_id="console")

    assert interrupted.status == "approval_required"
    assert interrupted.approval_id == str(approval_id)
    assert interrupted.resume_available is True
    assert approval.turn_checkpoint["mode"] == "agent_tool_loop"
    assert approval.resume_state["mode"] == "resume_turn"

    resumed = runtime.resume_turn(db, approval_id, decision="approve")

    assert resumed.status == "completed"
    assert resumed.answer == "sent after approval"
    assert approval.status == "approved"
    assert executor.calls[-1]["approved"] is True
