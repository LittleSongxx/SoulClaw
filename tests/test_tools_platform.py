from __future__ import annotations

import uuid

import pytest

from backend.domain.platform import PlatformService
from backend.domain.tools import ToolDefinition, ToolExecutor, ToolRegistry, ToolSafetyFloor


class DummyWiki:
    def search(self, db, query: str, limit: int = 10):
        return []

    def read(self, db, page_key: str):
        return None

    def compile(self, db):
        return {"status": "ok"}


class DummyMemory:
    def search(self, db, query: str, limit: int = 10):
        return []

    def create(self, db, **kwargs):
        return type("Memory", (), {"id": uuid.uuid4(), "kind": kwargs["kind"], "content": kwargs["content"]})()


class DummySkills:
    def scan(self, db):
        return {"skills": 0}

    def test(self, db, skill_key: str):
        return {"ok": True, "skill_key": skill_key}


class FakeDB:
    def __init__(self) -> None:
        self.objects = []

    def add(self, item) -> None:
        self.objects.append(item)

    def flush(self) -> None:
        for item in self.objects:
            if getattr(item, "id", None) is None:
                item.id = uuid.uuid4()

    def scalar(self, statement):
        text = str(statement)
        if "cron_jobs" in text:
            return next((item for item in self.objects if item.__class__.__name__ == "CronJob"), None)
        return None


def test_tool_safety_floor_blocks_destructive_arguments() -> None:
    tool = ToolDefinition(name="shell", description="shell", scope="system.write")

    with pytest.raises(PermissionError):
        ToolSafetyFloor().validate(tool, {"cmd": "rm -rf /"})


def test_tool_executor_records_successful_run() -> None:
    registry = ToolRegistry(wiki=DummyWiki(), memory=DummyMemory(), skills=DummySkills())
    executor = ToolExecutor(registry)
    db = FakeDB()

    result = executor.execute(db, tool_name="wiki_compile", arguments={})

    assert result["status"] == "succeeded"
    assert any(getattr(item, "tool_name", None) == "wiki_compile" for item in db.objects)


def test_platform_service_validates_cron_expr() -> None:
    service = PlatformService()

    with pytest.raises(ValueError):
        service.upsert_cron_job(FakeDB(), name="bad", cron_expr="not cron")


def test_platform_service_validates_mcp_transport_requirements() -> None:
    service = PlatformService()

    with pytest.raises(ValueError):
        service.upsert_mcp_server(FakeDB(), name="remote", transport="sse")


def test_platform_service_validates_gateway_kind() -> None:
    service = PlatformService()

    with pytest.raises(ValueError):
        service.upsert_gateway(FakeDB(), name="unknown", kind="irc")


def test_platform_upserts_flush_new_records() -> None:
    db = FakeDB()
    service = PlatformService()

    job = service.upsert_cron_job(db, name="daily", cron_expr="0 3 * * *", enabled=False)

    assert job.id is not None


def test_platform_cron_upsert_sets_next_run_when_enabled() -> None:
    db = FakeDB()
    service = PlatformService()

    job = service.upsert_cron_job(db, name="daily", cron_expr="0 3 * * *", enabled=True)

    assert job.next_run_at is not None


def test_platform_system_cron_preserves_disabled_state() -> None:
    db = FakeDB()
    service = PlatformService()
    job = service.ensure_system_cron_job(
        db,
        name="system-dream-review",
        cron_expr="30 3 * * *",
        timezone="Asia/Shanghai",
        instruction="Run Dream review.",
        metadata={"system_task": "dream_review"},
        enabled=True,
    )
    job.enabled = False
    job.next_run_at = None

    updated = service.ensure_system_cron_job(
        db,
        name="system-dream-review",
        cron_expr="30 3 * * *",
        timezone="Asia/Shanghai",
        instruction="Run Dream review.",
        metadata={"system_task": "dream_review"},
        enabled=True,
    )

    assert updated is job
    assert updated.enabled is False
    assert updated.next_run_at is None


def test_tool_executor_creates_approval_for_gated_tool() -> None:
    registry = ToolRegistry(wiki=DummyWiki(), memory=DummyMemory(), skills=DummySkills())
    registry.register(
        ToolDefinition(
            name="dangerous",
            description="dangerous",
            scope="system.write",
            requires_approval=True,
            handler=lambda db, args: {"ok": True},
        )
    )
    db = FakeDB()
    executor = ToolExecutor(registry, platform=PlatformService())

    with pytest.raises(PermissionError):
        executor.execute(db, tool_name="dangerous", arguments={"value": 1})

    assert any(getattr(item, "subject_type", None) == "tool_run" for item in db.objects)
