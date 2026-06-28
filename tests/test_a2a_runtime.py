from __future__ import annotations

import json
import uuid

import pytest

from backend.domain.a2a import A2AService
from backend.domain.tools import ToolExecutor, ToolRegistry
from backend.infra.config import Settings
from backend.infra.models import A2AAgentConnection, A2AArtifact, A2AEvent, A2ATask, ToolRun
from backend.runtime.a2a import A2ADelegateRequest, A2ARuntimeManager


class DummyEvents:
    def __init__(self) -> None:
        self.events = []
        self.audits = []

    def emit(self, event_type, payload=None, **kwargs) -> None:
        self.events.append((event_type, payload or {}, kwargs))

    def audit(self, action, target_type, **kwargs) -> None:
        self.audits.append((action, target_type, kwargs))


class FakeScalarResult:
    def __init__(self, values) -> None:
        self.values = values

    def all(self):
        return self.values


class FakeDB:
    def __init__(self) -> None:
        self.objects = []

    def add(self, item) -> None:
        self.objects.append(item)

    def flush(self) -> None:
        for item in self.objects:
            if getattr(item, "id", None) is None:
                item.id = uuid.uuid4()

    def get(self, model, item_id):
        for item in self.objects:
            if isinstance(item, model) and item.id == item_id:
                return item
        return None

    def scalar(self, statement):
        text = str(statement)
        if "a2a_agent_connections" in text:
            if "WHERE a2a_agent_connections.name =" in text:
                target = _extract_bound_value(statement, "name_1")
                for item in self.objects:
                    if isinstance(item, A2AAgentConnection) and item.name == target:
                        return item
                return None
            for item in self.objects:
                if isinstance(item, A2AAgentConnection):
                    return item
            return None
        if "a2a_tasks" in text:
            target = (
                _extract_bound_value(statement, "task_id_1")
                or _extract_bound_value(statement, "remote_task_id_1")
                or _extract_bound_value(statement, "remote_context_id_1")
                or _extract_bound_value(statement, "id_1")
            )
            for item in self.objects:
                if (
                    isinstance(item, A2ATask)
                    and target in {item.task_id, item.remote_task_id, item.remote_context_id}
                ):
                    return item
            return None
        if "a2a_events" in text:
            task_id = _extract_bound_value(statement, "task_id_1")
            events = [item for item in self.objects if isinstance(item, A2AEvent) and item.task_id == task_id]
            return sorted(events, key=lambda item: item.sequence, reverse=True)[0] if events else None
        return None

    def scalars(self, statement):
        text = str(statement)
        if "a2a_agent_connections" in text:
            values = [item for item in self.objects if isinstance(item, A2AAgentConnection)]
            return FakeScalarResult(sorted(values, key=lambda item: item.name))
        if "a2a_tasks" in text:
            values = [item for item in self.objects if isinstance(item, A2ATask)]
            return FakeScalarResult(values)
        if "a2a_artifacts" in text:
            task_id = _extract_bound_value(statement, "task_id_1")
            values = [item for item in self.objects if isinstance(item, A2AArtifact) and item.task_id == task_id]
            return FakeScalarResult(values)
        if "a2a_events" in text:
            task_id = _extract_bound_value(statement, "task_id_1")
            values = [item for item in self.objects if isinstance(item, A2AEvent) and item.task_id == task_id]
            return FakeScalarResult(sorted(values, key=lambda item: item.sequence))
        return FakeScalarResult([])


def _extract_bound_value(statement, key: str):
    compiled = statement.compile()
    return compiled.params.get(key)


def _runtime(db: FakeDB) -> A2ARuntimeManager:
    service = A2AService(events=DummyEvents())
    service.upsert_connection(
        db,
        name="weaver-deep-research",
        kind="weaver",
        endpoint="http://weaver.test",
        capabilities=["deep-research", "research", "weaver"],
        skills=[{"id": "deep-research", "name": "Deep Research", "tags": ["research"]}],
    )
    return A2ARuntimeManager(service=service, events=DummyEvents(), settings=Settings())


def test_agent_card_exposes_a2a_interfaces() -> None:
    runtime = A2ARuntimeManager(service=A2AService(), events=DummyEvents(), settings=Settings(public_base_url="http://soulclaw.test"))

    card = runtime.agent_card()

    assert card["name"] == "SoulClaw"
    assert card["url"] == "http://soulclaw.test/api/a2a"
    assert any(skill["id"] == "deep-research" for skill in card["skills"])


def test_weaver_discovery_builds_adapter_card() -> None:
    db = FakeDB()
    runtime = _runtime(db)

    card = runtime.discover(db, "weaver-deep-research")

    assert card["name"] == "Weaver DeepResearch"
    assert "deep-research" in db.objects[0].capabilities
    assert db.objects[0].status == "online"


def test_jsonrpc_tasks_resubscribe_returns_persisted_events() -> None:
    db = FakeDB()
    runtime = _runtime(db)
    task = runtime.service.create_task(
        db,
        connection_name="weaver-deep-research",
        capability="deep-research",
        input_text="research",
    )
    runtime.service.add_event(db, task.task_id, "custom.progress", {"ok": True})

    response = runtime.handle_jsonrpc(
        db,
        {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tasks/resubscribe",
            "params": {"id": task.task_id, "afterSequence": 1},
        },
    )

    assert response["result"]["task"]["metadata"]["localTaskId"] == task.task_id
    assert any(item["type"] == "custom.progress" for item in response["result"]["events"])


def test_jsonrpc_tasks_get_accepts_remote_task_id() -> None:
    db = FakeDB()
    runtime = _runtime(db)
    task = runtime.service.create_task(
        db,
        connection_name="weaver-deep-research",
        capability="deep-research",
        input_text="research",
    )
    runtime.service.mark_task(db, task, status="working", remote_task_id="remote-task-1", remote_context_id="remote-ctx-1")

    response = runtime.handle_jsonrpc(
        db,
        {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tasks/get",
            "params": {"id": "remote-task-1"},
        },
    )

    assert response["result"]["metadata"]["localTaskId"] == task.task_id
    assert response["result"]["id"] == "remote-task-1"


def test_weaver_cancel_event_accepts_us_spelling() -> None:
    mapped = A2ARuntimeManager._map_weaver_event({"type": "canceled"})

    assert mapped["status"] == "canceled"
    assert mapped["terminal"] is True


def test_weaver_sse_delegation_maps_events_and_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB()
    runtime = _runtime(db)
    frames = [
        {"type": "brief_created", "data": {"thread_id": "thread-1"}},
        {"type": "artifact", "data": {"artifact_id": "draft", "content": "draft report"}},
        {"type": "completion", "data": {"content": "final report"}},
        {"type": "done", "data": {"thread_id": "thread-1"}},
    ]

    class FakeStreamResponse:
        headers = {"X-Thread-ID": "thread-1"}
        status_code = 200

        def raise_for_status(self):
            return None

        def iter_lines(self):
            for frame in frames:
                yield f"event: {frame['type']}"
                yield "data: " + json.dumps(frame)
                yield ""

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeResponse:
        status_code = 404

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def stream(self, *args, **kwargs):
            return FakeStreamResponse()

        def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("backend.runtime.a2a.httpx.Client", FakeClient)

    result = runtime.delegate(db, A2ADelegateRequest(capability="deep-research", query="deep research this"))

    assert result["status"] == "completed"
    assert result["answer"] == "final report"
    artifacts = [item for item in db.objects if isinstance(item, A2AArtifact)]
    assert any(item.artifact_id == "draft" for item in artifacts)
    assert any(item.artifact_id == "weaver-final-answer" for item in artifacts)


def test_weaver_interrupt_stays_input_required(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB()
    runtime = _runtime(db)

    class FakeStreamResponse:
        headers = {"X-Thread-ID": "thread-interrupt"}

        def raise_for_status(self):
            return None

        def iter_lines(self):
            yield "event: interrupt"
            yield 'data: {"type":"interrupt","data":{"message":"approve plan"}}'
            yield ""

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def stream(self, *args, **kwargs):
            return FakeStreamResponse()

    monkeypatch.setattr("backend.runtime.a2a.httpx.Client", FakeClient)

    result = runtime.delegate(db, A2ADelegateRequest(capability="deep-research", query="deep research this"))

    assert result["status"] == "input-required"
    task = next(item for item in db.objects if isinstance(item, A2ATask))
    assert task.status == "input-required"


def test_a2a_delegate_tool_dynamic_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB()
    runtime = _runtime(db)

    class DummyWiki:
        pass

    class DummyMemory:
        pass

    class DummySkills:
        pass

    registry = ToolRegistry(wiki=DummyWiki(), memory=DummyMemory(), skills=DummySkills(), events=DummyEvents())
    registry.install_a2a(runtime)

    class DummyPlatform:
        def create_approval(self, db, *, subject_type, subject_id="", payload=None):
            return type("Approval", (), {"id": uuid.uuid4()})()

    executor = ToolExecutor(registry, events=DummyEvents(), platform=DummyPlatform())

    with pytest.raises(PermissionError):
        executor.execute(
            db,
            tool_name="a2a_delegate",
            arguments={"capability": "code-writing", "query": "change repo"},
        )

    run = next(item for item in db.objects if isinstance(item, ToolRun))
    assert run.status == "approval_required"


def test_a2a_delegate_tool_runs_after_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB()
    runtime = _runtime(db)
    monkeypatch.setattr(
        runtime,
        "delegate",
        lambda db, request: {"task_id": "approved-task", "status": "completed", "capability": request.capability},
    )

    class DummyWiki:
        pass

    class DummyMemory:
        pass

    class DummySkills:
        pass

    registry = ToolRegistry(wiki=DummyWiki(), memory=DummyMemory(), skills=DummySkills(), events=DummyEvents())
    registry.install_a2a(runtime)
    executor = ToolExecutor(registry, events=DummyEvents())

    result = executor.execute(
        db,
        tool_name="a2a_delegate",
        arguments={"capability": "code-writing", "query": "change repo"},
        approved=True,
    )

    assert result["status"] == "succeeded"
    assert result["result"]["task_id"] == "approved-task"
