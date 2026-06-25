from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

from backend.runtime.gateway import (
    GatewayRuntimeManager,
    InboundGatewayMessage,
    OutboundGatewayMessage,
)


class DummyEvents:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event_type, payload=None, **kwargs) -> None:
        self.events.append((event_type, payload or {}, kwargs))


class DummyAgent:
    def run_turn(self, db, message: str, *, session_id: str = "local"):
        return type("Result", (), {"turn_id": "turn-1", "answer": f"reply: {message}", "context": {}})()


class DummyGateway:
    def __init__(self, *, enabled: bool = True, kind: str = "local") -> None:
        self.id = uuid.uuid4()
        self.name = "local"
        self.kind = kind
        self.enabled = enabled
        self.endpoint = ""
        self.status = "enabled"
        self.config = {}
        self.last_inbound_at = None
        self.last_outbound_at = None
        self.last_error = ""
        self.inbound_count = 0
        self.outbound_count = 0
        self.failure_count = 0


class FakeDB:
    def __init__(self, gateway: DummyGateway) -> None:
        self.gateway = gateway

    def scalar(self, statement):
        del statement
        return self.gateway


@contextmanager
def fake_session_scope(gateway: DummyGateway):
    yield FakeDB(gateway)


def test_gateway_inbound_invokes_agent_and_tracks_state(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = DummyGateway()
    monkeypatch.setattr("backend.runtime.gateway.session_scope", lambda: fake_session_scope(gateway))
    manager = GatewayRuntimeManager(agent=DummyAgent(), events=DummyEvents())

    result = manager.handle_inbound(
        InboundGatewayMessage(gateway_name="local", external_user_id="user-1", channel_id="dm", text="hello")
    )

    assert result["answer"] == "reply: hello"
    assert result["session_id"] == "gateway:local:dm:user-1"
    assert gateway.inbound_count == 1
    assert gateway.status == "online"


def test_gateway_local_send_tracks_outbound_state(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = DummyGateway()
    monkeypatch.setattr("backend.runtime.gateway.session_scope", lambda: fake_session_scope(gateway))
    manager = GatewayRuntimeManager(agent=DummyAgent(), events=DummyEvents())

    result = manager.send(OutboundGatewayMessage(gateway_name="local", target_id="user-1", text="hi"))

    assert result["mode"] == "local"
    assert gateway.outbound_count == 1
    assert gateway.status == "online"


def test_gateway_disabled_connection_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = DummyGateway(enabled=False)
    monkeypatch.setattr("backend.runtime.gateway.session_scope", lambda: fake_session_scope(gateway))
    manager = GatewayRuntimeManager(agent=DummyAgent(), events=DummyEvents())

    with pytest.raises(RuntimeError):
        manager.send(OutboundGatewayMessage(gateway_name="local", target_id="user-1", text="hi"))
