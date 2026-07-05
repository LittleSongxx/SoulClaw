from __future__ import annotations

import json
import uuid

import pytest

from backend.api.admin.a2a import _a2a_public_authorized, a2a_jsonrpc, get_a2a_task
from backend.domain.a2a import A2AService
from backend.domain.platform import PlatformService
from backend.domain.policy import ToolPolicyEngine
from backend.domain.tools import ToolApprovalRequired, ToolExecutor, ToolRegistry
from backend.infra.config import Settings
from backend.infra.models import (
    A2AAgentConnection,
    A2AArtifact,
    A2AEvent,
    A2ATask,
    Approval,
    ToolRun,
)
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
        name="soulsearcher-deep-research",
        kind="a2a",
        endpoint="http://soulsearcher.test",
        rpc_url="http://soulsearcher.test/api/a2a",
        agent_card={
            "name": "SoulSearcher DeepResearch",
            "capabilities": {"streaming": True},
            "defaultOutputModes": ["text/markdown", "application/json"],
        },
        config={"user_id": "soulclaw", "auth_user_header": "X-SoulSearcher-User"},
        capabilities=["deep-research", "research", "soulsearcher"],
        skills=[{"id": "deep-research", "name": "Deep Research", "tags": ["research"]}],
    )
    return A2ARuntimeManager(service=service, events=DummyEvents(), settings=Settings())


def test_agent_card_exposes_a2a_interfaces() -> None:
    runtime = A2ARuntimeManager(service=A2AService(), events=DummyEvents(), settings=Settings(public_base_url="http://soulclaw.test"))

    card = runtime.agent_card()

    assert card["name"] == "SoulClaw"
    assert card["url"] == "http://soulclaw.test/api/a2a"
    assert card["supportedInterfaces"] == [
        {
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
            "url": "http://soulclaw.test/api/a2a",
        }
    ]
    assert any(skill["id"] == "deep-research" for skill in card["skills"])


def test_soulsearcher_deep_research_delegate_metadata_contract() -> None:
    db = FakeDB()
    runtime = _runtime(db)
    task = runtime.service.create_task(
        db,
        task_id="a2a_task_local",
        connection_name="soulsearcher-deep-research",
        capability="deep-research",
        input_text="contract research",
        context_id="ctx-local",
    )
    request = A2ADelegateRequest(
        capability="deep-research",
        query="contract research",
        context={
            "session_id": "local",
            "turn_id": "turn-1",
        },
        options={
            "client_request_id": "turn-1:deep-research",
            "model": "contract-model",
            "retrieval_policy": {"preserve_evidence": True},
            "skill_ids": ["deep-research"],
            "deepsearch_config": {"deepsearch_max_seconds": 30},
            "user_id": "soulclaw",
        },
    )

    metadata = runtime._delegate_metadata(task, request)
    message = runtime._message(
        "user",
        request.query,
        context_id=task.context_id,
        metadata=metadata,
    )

    assert metadata["capability"] == "deep-research"
    assert metadata["soulclaw_task_id"] == "a2a_task_local"
    assert metadata["client_request_id"] == "turn-1:deep-research"
    assert metadata["idempotency_key"] == "turn-1:deep-research"
    assert metadata["user_id"] == "soulclaw"
    assert metadata["session_id"] == "local"
    assert metadata["turn_id"] == "turn-1"
    assert metadata["options"]["retrieval_policy"] == {"preserve_evidence": True}
    assert metadata["options"]["skill_ids"] == ["deep-research"]
    assert metadata["options"]["deepsearch_config"] == {"deepsearch_max_seconds": 30}
    assert message["metadata"] == metadata
    assert message["contextId"] == "ctx-local"



def test_jsonrpc_rejects_non_current_method_names() -> None:
    db = FakeDB()
    runtime = _runtime(db)

    response = runtime.handle_jsonrpc(
        db,
        {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "TaskResubscribe",
            "params": {"id": "task-1", "afterSequence": 1},
        },
    )

    assert response["error"]["code"] == -32601

def test_jsonrpc_subscribe_to_task_returns_persisted_events() -> None:
    db = FakeDB()
    runtime = _runtime(db)
    task = runtime.service.create_task(
        db,
        connection_name="soulsearcher-deep-research",
        capability="deep-research",
        input_text="research",
    )
    runtime.service.add_event(db, task.task_id, "custom.progress", {"ok": True})

    response = runtime.handle_jsonrpc(
        db,
        {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "SubscribeToTask",
            "params": {"id": task.task_id, "afterSequence": 1},
        },
    )

    assert response["result"]["task"]["metadata"]["localTaskId"] == task.task_id
    assert any(item["type"] == "custom.progress" for item in response["result"]["events"])


def test_jsonrpc_get_task_accepts_remote_task_id() -> None:
    db = FakeDB()
    runtime = _runtime(db)
    task = runtime.service.create_task(
        db,
        connection_name="soulsearcher-deep-research",
        capability="deep-research",
        input_text="research",
    )
    runtime.service.mark_task(db, task, status="working", remote_task_id="remote-task-1", remote_context_id="remote-ctx-1")

    response = runtime.handle_jsonrpc(
        db,
        {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "GetTask",
            "params": {"id": "remote-task-1"},
        },
    )

    assert response["result"]["metadata"]["localTaskId"] == task.task_id
    assert response["result"]["id"] == "remote-task-1"
    assert response["result"]["contextId"] == "remote-ctx-1"
    assert response["result"]["history"][0]["taskId"] == "remote-task-1"
    assert response["result"]["history"][0]["contextId"] == "remote-ctx-1"


def test_admin_get_task_by_remote_id_uses_local_id_for_artifacts_and_events() -> None:
    db = FakeDB()
    runtime = _runtime(db)
    task = runtime.service.create_task(
        db,
        connection_name="soulsearcher-deep-research",
        capability="deep-research",
        input_text="research",
    )
    runtime.service.mark_task(db, task, status="completed", remote_task_id="remote-task-1", remote_context_id="remote-ctx-1")
    runtime.service.add_artifact(db, task.task_id, artifact_id="report", content="final report")
    runtime.service.add_event(db, task.task_id, "custom.progress", {"ok": True})

    result = get_a2a_task("remote-task-1", db=db, service=runtime.service)

    assert result["task_id"] == task.task_id
    assert any(item["artifact_id"] == "report" for item in result["artifacts"])
    assert any(item["event_type"] == "custom.progress" for item in result["events"])


def test_a2a_discovery_prefers_current_jsonrpc_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB()
    service = A2AService(events=DummyEvents())
    service.upsert_connection(
        db,
        name="soulsearcher-deep-research",
        kind="a2a",
        endpoint="http://soulsearcher.test",
        capabilities=["deep-research"],
    )
    runtime = A2ARuntimeManager(service=service, events=DummyEvents(), settings=Settings())
    card = {
        "name": "SoulSearcher DeepResearch",
        "supportedInterfaces": [
            {"protocolBinding": "JSONRPC", "protocolVersion": "1.0", "url": "http://new.test/api/a2a"},
        ],
        "skills": [{"id": "deep-research", "name": "Deep Research", "tags": ["research"]}],
    }

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return card

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("backend.runtime.a2a.httpx.Client", FakeClient)

    runtime.discover(db, "soulsearcher-deep-research")

    connection = runtime.service.get_connection(db, "soulsearcher-deep-research")
    assert connection is not None
    assert connection.rpc_url == "http://new.test/api/a2a"


def test_a2a_discovery_rejects_non_current_jsonrpc_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB()
    service = A2AService(events=DummyEvents())
    service.upsert_connection(
        db,
        name="old-agent",
        kind="a2a",
        endpoint="http://old.test",
        capabilities=["deep-research"],
    )
    runtime = A2ARuntimeManager(service=service, events=DummyEvents(), settings=Settings())

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "name": "Old Agent",
                "supportedInterfaces": [
                    {"protocolBinding": "JSONRPC", "protocolVersion": "0.3", "url": "http://old.test/api/a2a"}
                ],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("backend.runtime.a2a.httpx.Client", FakeClient)

    with pytest.raises(RuntimeError, match="A2A discovery failed"):
        runtime.discover(db, "old-agent")


def test_a2a_streaming_delegation_sends_current_method_and_persists_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB()
    service = A2AService(events=DummyEvents())
    service.upsert_connection(
        db,
        name="soulsearcher-deep-research",
        kind="a2a",
        endpoint="http://soulsearcher.test",
        rpc_url="http://soulsearcher.test/api/a2a",
        agent_card={
            "name": "SoulSearcher DeepResearch",
            "capabilities": {"streaming": True},
            "defaultOutputModes": ["text/markdown", "application/json"],
        },
        config={"user_id": "soulclaw", "auth_user_header": "X-SoulSearcher-User"},
        capabilities=["deep-research"],
        skills=[{"id": "deep-research", "name": "Deep Research", "tags": ["research"]}],
    )
    runtime = A2ARuntimeManager(service=service, events=DummyEvents(), settings=Settings())
    calls: list[dict] = []
    frames = [
        {
            "jsonrpc": "2.0",
            "id": "rpc-1",
            "result": {
                "task": {
                    "id": "remote-task-1",
                    "contextId": "remote-ctx-1",
                    "status": {"state": "TASK_STATE_SUBMITTED"},
                }
            },
        },
        {
            "jsonrpc": "2.0",
            "id": "rpc-1",
            "result": {
                "statusUpdate": {
                    "taskId": "remote-task-1",
                    "contextId": "remote-ctx-1",
                    "status": {"state": "TASK_STATE_WORKING"},
                }
            },
        },
        {
            "jsonrpc": "2.0",
            "id": "rpc-1",
            "result": {
                "artifactUpdate": {
                    "taskId": "remote-task-1",
                    "contextId": "remote-ctx-1",
                    "artifact": {
                        "artifactId": "final-report",
                        "name": "Research Report",
                        "parts": [{"text": "final report", "mediaType": "text/markdown"}],
                    },
                    "lastChunk": True,
                }
            },
        },
        {
            "jsonrpc": "2.0",
            "id": "rpc-1",
            "result": {
                "statusUpdate": {
                    "taskId": "remote-task-1",
                    "contextId": "remote-ctx-1",
                    "status": {
                        "state": "TASK_STATE_COMPLETED",
                        "message": {"role": "ROLE_AGENT", "parts": [{"text": "final report"}]},
                    },
                }
            },
        },
    ]

    class FakeStreamResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def iter_lines(self):
            for frame in frames:
                yield "data: " + json.dumps(frame)
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

        def stream(self, method, url, *, json=None, headers=None):
            calls.append({"method": method, "url": url, "json": json, "headers": headers})
            return FakeStreamResponse()

    monkeypatch.setattr("backend.runtime.a2a.httpx.Client", FakeClient)

    result = runtime.delegate(db, A2ADelegateRequest(capability="deep-research", query="deep research this"))

    assert calls[0]["json"]["method"] == "SendStreamingMessage"
    assert calls[0]["json"]["params"]["message"]["parts"] == [{"text": "deep research this"}]
    assert calls[0]["json"]["params"]["message"]["role"] == "ROLE_USER"
    assert calls[0]["headers"]["A2A-Version"] == "1.0"
    assert result["status"] == "completed"
    assert result["remote_task_id"] == "remote-task-1"
    assert result["answer"] == "final report"
    task = next(item for item in db.objects if isinstance(item, A2ATask))
    assert task.remote_task_id == "remote-task-1"
    assert task.remote_context_id == "remote-ctx-1"
    artifacts = [item for item in db.objects if isinstance(item, A2AArtifact)]
    assert any(item.artifact_id == "final-report" and item.content == "final report" for item in artifacts)
    events = [item for item in db.objects if isinstance(item, A2AEvent)]
    assert any(item.event_type == "a2a.status_update" for item in events)
    assert any(item.event_type == "a2a.artifact_update" for item in events)


def test_a2a_input_required_creates_remote_hitl_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB()
    runtime = _runtime(db)
    runtime.platform = PlatformService(events=DummyEvents())
    frames = [
        {
            "jsonrpc": "2.0",
            "id": "rpc-1",
            "result": {
                "task": {
                    "id": "remote-task-hitl",
                    "contextId": "remote-ctx-hitl",
                    "status": {"state": "TASK_STATE_SUBMITTED"},
                }
            },
        },
        {
            "jsonrpc": "2.0",
            "id": "rpc-1",
            "result": {
                "statusUpdate": {
                    "taskId": "remote-task-hitl",
                    "contextId": "remote-ctx-hitl",
                    "status": {
                        "state": "TASK_STATE_INPUT_REQUIRED",
                        "message": {"role": "ROLE_AGENT", "parts": [{"text": "approve plan"}]},
                    },
                    "metadata": {
                        "hitl": {
                            "kind": "a2a_remote_hitl",
                            "message": "approve plan",
                            "allowed_decisions": ["approve", "reject"],
                            "action_requests": [{"name": "search", "args": {"q": "x"}}],
                        }
                    },
                }
            },
        },
    ]

    class FakeStreamResponse:
        def raise_for_status(self):
            return None
        def iter_lines(self):
            for frame in frames:
                yield "data: " + json.dumps(frame)
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

    result = runtime.delegate(db, A2ADelegateRequest(capability="deep-research", query="research"))

    assert result["status"] == "input-required"
    approvals = [item for item in db.objects if isinstance(item, Approval)]
    assert approvals
    assert approvals[0].subject_type == "a2a_remote_hitl"
    assert approvals[0].payload["hitl"]["message"] == "approve plan"


def test_a2a_remote_approval_resume_sends_followup_task_message(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB()
    runtime = _runtime(db)
    runtime.platform = PlatformService(events=DummyEvents())
    task = runtime.service.create_task(
        db,
        connection_name="soulsearcher-deep-research",
        capability="deep-research",
        input_text="research",
    )
    runtime.service.mark_task(db, task, status="input-required", remote_task_id="remote-task-1", remote_context_id="remote-ctx-1")
    approval = runtime.platform.create_approval(
        db,
        subject_type="a2a_remote_hitl",
        subject_id=task.task_id,
        payload={"hitl": {"action_requests": [{"name": "search", "args": {"q": "x"}}]}},
        resume_state={"hitl": {"action_requests": [{"name": "search", "args": {"q": "x"}}]}},
    )
    calls: list[dict] = []
    frames = [
        {
            "jsonrpc": "2.0",
            "id": "rpc-1",
            "result": {
                "statusUpdate": {
                    "taskId": "remote-task-1",
                    "contextId": "remote-ctx-1",
                    "status": {"state": "TASK_STATE_COMPLETED", "message": {"role": "ROLE_AGENT", "parts": [{"text": "done"}]}},
                }
            },
        }
    ]

    class FakeStreamResponse:
        def raise_for_status(self):
            return None
        def iter_lines(self):
            for frame in frames:
                yield "data: " + json.dumps(frame)
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
        def stream(self, method, url, *, json=None, headers=None):
            calls.append({"method": method, "url": url, "json": json, "headers": headers})
            return FakeStreamResponse()

    monkeypatch.setattr("backend.runtime.a2a.httpx.Client", FakeClient)

    result = runtime.resume_remote_approval(db, approval.id, decision="approve")

    message = calls[0]["json"]["params"]["message"]
    assert message["taskId"] == "remote-task-1"
    assert message["contextId"] == "remote-ctx-1"
    assert json.loads(message["parts"][0]["text"])["tool_approved"] is True
    assert result["result"]["status"] == "completed"
    assert approval.status == "approved"


def test_a2a_callback_updates_task_and_creates_hitl_approval() -> None:
    db = FakeDB()
    runtime = _runtime(db)
    runtime.platform = PlatformService(events=DummyEvents())
    task = runtime.service.create_task(
        db,
        connection_name="soulsearcher-deep-research",
        capability="deep-research",
        input_text="research",
    )
    runtime.service.mark_task(db, task, status="working", remote_task_id="remote-task-1", remote_context_id="remote-ctx-1")

    result = runtime.handle_callback(
        db,
        {
            "event": "task.input_required",
            "task_id": "remote-task-1",
            "context_id": "remote-ctx-1",
            "metadata": {"soulclaw_task_id": task.task_id},
            "hitl": {"kind": "a2a_remote_hitl", "message": "approve plan", "allowed_decisions": ["approve"]},
        },
    )

    assert result["status"] == "input-required"
    approvals = [item for item in db.objects if isinstance(item, Approval)]
    assert approvals
    assert approvals[0].subject_type == "a2a_remote_hitl"


def test_a2a_sync_active_remote_tasks_polls_working_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB()
    runtime = _runtime(db)
    task = runtime.service.create_task(
        db,
        connection_name="soulsearcher-deep-research",
        capability="deep-research",
        input_text="research",
    )
    runtime.service.mark_task(db, task, status="working", remote_task_id="remote-task-1", remote_context_id="remote-ctx-1")

    def fake_call_jsonrpc(connection, method, params):
        assert method == "GetTask"
        assert params == {"id": "remote-task-1"}
        return {
            "jsonrpc": "2.0",
            "result": {
                "id": "remote-task-1",
                "contextId": "remote-ctx-1",
                "status": {
                    "state": "TASK_STATE_COMPLETED",
                    "message": {"role": "ROLE_AGENT", "parts": [{"text": "final report"}]},
                },
            },
        }

    monkeypatch.setattr(runtime, "_call_jsonrpc", fake_call_jsonrpc)

    result = runtime.sync_active_remote_tasks(db)

    assert result["synced"] == 1
    assert result["errors"] == []
    assert task.status == "completed"
    assert task.result["answer"] == "final report"


def test_a2a_delegation_falls_back_to_send_message_when_streaming_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB()
    service = A2AService(events=DummyEvents())
    service.upsert_connection(
        db,
        name="batch-research",
        kind="a2a",
        endpoint="http://batch.test",
        rpc_url="http://batch.test/api/a2a",
        agent_card={"name": "Batch Research", "capabilities": {"streaming": False}},
        capabilities=["deep-research"],
        skills=[{"id": "deep-research", "name": "Deep Research", "tags": ["research"]}],
    )
    runtime = A2ARuntimeManager(service=service, events=DummyEvents(), settings=Settings())
    calls: list[dict] = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "jsonrpc": "2.0",
                "id": "rpc-1",
                "result": {
                    "task": {
                        "id": "remote-task-2",
                        "contextId": "remote-ctx-2",
                        "status": {
                            "state": "TASK_STATE_COMPLETED",
                            "message": {"role": "ROLE_AGENT", "parts": [{"text": "batch final"}]},
                        },
                        "artifacts": [
                            {
                                "artifactId": "batch-report",
                                "name": "Batch Report",
                                "parts": [{"text": "batch final", "mediaType": "text/markdown"}],
                            }
                        ],
                    }
                },
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, *, json=None, headers=None):
            calls.append({"url": url, "json": json, "headers": headers})
            return FakeResponse()

    monkeypatch.setattr("backend.runtime.a2a.httpx.Client", FakeClient)

    result = runtime.delegate(
        db,
        A2ADelegateRequest(
            capability="deep-research",
            query="deep research this",
            connection_name="batch-research",
        ),
    )

    assert calls[0]["json"]["method"] == "SendMessage"
    assert result["status"] == "completed"
    assert result["answer"] == "batch final"
    artifacts = [item for item in db.objects if isinstance(item, A2AArtifact)]
    assert any(item.artifact_id == "batch-report" for item in artifacts)


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

    with pytest.raises(ToolApprovalRequired):
        executor.execute(
            db,
            tool_name="a2a_delegate",
            arguments={"capability": "code-writing", "query": "change repo"},
        )

    assert not any(isinstance(item, ToolRun) for item in db.objects)


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


def test_a2a_delegate_tool_policy_can_allow_direct_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB()
    runtime = _runtime(db)
    monkeypatch.setattr(
        runtime,
        "delegate",
        lambda db, request: {"task_id": "direct-task", "status": "completed", "capability": request.capability},
    )

    class DummyWiki:
        pass

    class DummyMemory:
        pass

    class DummySkills:
        pass

    class DirectPolicy(ToolPolicyEngine):
        def decide(self, db, *, tool_name, scope, arguments=None, declared_requires_approval=False):
            decision = super().decide(
                db,
                tool_name=tool_name,
                scope=scope,
                arguments=arguments,
                declared_requires_approval=declared_requires_approval,
            )
            return type(decision)(True, False, decision.risk_level, "test direct allow", scope=scope)

    registry = ToolRegistry(wiki=DummyWiki(), memory=DummyMemory(), skills=DummySkills(), events=DummyEvents())
    registry.install_a2a(runtime)
    executor = ToolExecutor(registry, events=DummyEvents(), policy=DirectPolicy(events=DummyEvents()))

    result = executor.execute(
        db,
        tool_name="a2a_delegate",
        arguments={"capability": "deep-research", "query": "research"},
    )

    assert result["status"] == "succeeded"
    assert result["result"]["task_id"] == "direct-task"


def test_a2a_public_auth_requires_key_when_required_without_public_base_url() -> None:
    settings = type(
        "Settings",
        (),
        {"a2a_require_public_auth": True, "public_base_url": "", "a2a_public_api_key": "secret"},
    )()

    assert _a2a_public_authorized(settings, authorization=None, api_key=None) is False
    assert _a2a_public_authorized(settings, authorization="Bearer secret", api_key=None) is True
    assert _a2a_public_authorized(settings, authorization=None, api_key="secret") is True
    assert _a2a_public_authorized(settings, authorization=None, api_key="bad") is False


def test_a2a_public_auth_allows_when_disabled() -> None:
    settings = type(
        "Settings",
        (),
        {"a2a_require_public_auth": False, "public_base_url": "", "a2a_public_api_key": ""},
    )()

    assert _a2a_public_authorized(settings, authorization=None, api_key=None) is True


def test_public_jsonrpc_requires_a2a_version_header() -> None:
    response = a2a_jsonrpc(
        {"jsonrpc": "2.0", "id": "1", "method": "ListTasks", "params": {}},
        authorization=None,
        a2a_version=None,
        x_soulclaw_a2a_key=None,
        db=FakeDB(),
        runtime=_runtime(FakeDB()),
        settings=Settings(public_base_url="http://soulclaw.test", a2a_public_api_key="secret"),
    )

    assert response["error"]["code"] == -32600
    assert "A2A-Version" in response["error"]["message"]


def test_public_jsonrpc_accepts_a2a_version_1_header() -> None:
    db = FakeDB()
    runtime = _runtime(db)
    response = a2a_jsonrpc(
        {"jsonrpc": "2.0", "id": "1", "method": "ListTasks", "params": {}},
        authorization=None,
        a2a_version="1.0",
        x_soulclaw_a2a_key=None,
        db=db,
        runtime=runtime,
        settings=Settings(a2a_require_public_auth=False),
    )

    assert response["result"] == {"tasks": []}
