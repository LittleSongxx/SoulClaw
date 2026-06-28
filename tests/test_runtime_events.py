from __future__ import annotations

import pytest

from backend.infra.events import RuntimeEventBus
from backend.infra.models import AuditEvent, RuntimeEvent


class BrokenSessionScope:
    def __enter__(self):
        raise AssertionError("event bus opened a new session")

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeDB:
    def __init__(self) -> None:
        self.objects = []

    def add(self, item) -> None:
        self.objects.append(item)


def test_event_bus_reuses_bound_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backend.infra.events.session_scope", lambda: BrokenSessionScope())
    bus = RuntimeEventBus()
    db = FakeDB()

    with bus.bind_session(db):
        bus.emit("test.event", {"value": object()})
        bus.audit("test.audit", "thing", payload={"value": object()})

    assert any(isinstance(item, RuntimeEvent) and item.event_type == "test.event" for item in db.objects)
    assert any(isinstance(item, AuditEvent) and item.action == "test.audit" for item in db.objects)
    runtime_event = next(item for item in db.objects if isinstance(item, RuntimeEvent))
    assert runtime_event.payload["value"].startswith("<object object at")
