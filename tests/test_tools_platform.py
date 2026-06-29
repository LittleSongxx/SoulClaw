from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from backend.domain.platform import PlatformService
from backend.domain.tools import ToolDefinition, ToolExecutor, ToolRegistry, ToolSafetyFloor


class DummyWiki:
    def orientation(self, db, recent_log_lines: int = 40):
        del db, recent_log_lines
        return {"index": "[[index]]", "pages": []}

    def search(self, db, query: str, limit: int = 10):
        page = type("Page", (), {"page_key": "index", "title": "Index", "summary": "Summary", "path": "index.md", "tags": [], "page_type": "note", "confidence": 0.8})()
        return [
            {
                "page": page,
                "page_key": "index",
                "title": "Index",
                "path": "index.md",
                "summary": "Summary",
                "tags": [],
                "page_type": "note",
                "confidence": 0.8,
                "source": "page_index",
                "score": 1.0,
            }
        ]

    def read(self, db, page_key: str):
        if page_key == "index":
            return type("Page", (), {"page_key": "index", "title": "Index", "summary": "Summary", "body": "Body", "metadata_json": {}})()
        return None

    def read_with_graph(self, db, page_key: str):
        del db, page_key
        return {"outlinks": [], "backlinks": []}

    def follow_links(self, db, page_key: str, *, direction: str = "out", limit: int = 50):
        del db, page_key, direction, limit
        return [{"dst_page_key": "other"}]

    def lint(self, db):
        del db
        return {"ok": True}

    def compile(self, db):
        return {"status": "ok"}


class DummyMemory:
    def search(self, db, query: str, limit: int = 10):
        return []

    def get(self, db, memory_id):
        return None

    def create(self, db, **kwargs):
        return type("Memory", (), {"id": uuid.uuid4(), "kind": kwargs["kind"], "content": kwargs["content"]})()


class DummySkills:
    def __init__(self) -> None:
        self.skill = type(
            "Skill",
            (),
            {
                "skill_key": "coding/debug",
                "name": "Debug",
                "description": "Debug systematically",
                "status": "active",
                "pinned": False,
                "metadata_json": {},
            },
        )()

    def search(self, db, query: str, limit: int = 10):
        del db, query, limit
        return [{"skill": self.skill, "source": "skill_index", "score": 1.0}]

    def get(self, db, skill_key: str):
        del db
        return self.skill if skill_key == "coding/debug" else None

    def files(self, db, skill_key: str):
        del db, skill_key
        return [
            type(
                "SkillFile",
                (),
                {"file_path": "SKILL.md", "checksum": "abc", "content": "# Debug\n\nUse a checklist."},
            )()
        ]

    def scan(self, db):
        return {"skills": 0}

    def test(self, db, skill_key: str):
        return {"ok": True, "skill_key": skill_key}


class DummyEvents:
    def __init__(self) -> None:
        self.events = []
        self.audits = []

    def emit(self, event_type, payload=None, **kwargs):
        self.events.append((event_type, payload or {}, kwargs))

    def audit(self, action, target_type, **kwargs):
        self.audits.append((action, target_type, kwargs))


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


def test_wiki_search_tool_returns_page_index_shape() -> None:
    registry = ToolRegistry(wiki=DummyWiki(), memory=DummyMemory(), skills=DummySkills())

    result = registry.get("wiki_search").handler(None, {"query": "index"})

    assert result["items"][0]["page_key"] == "index"
    assert result["items"][0]["summary"] == "Summary"
    assert "chunk_index" not in result["items"][0]
    assert "candidate_only" not in result["items"][0]


def test_wiki_read_tool_returns_page_graph() -> None:
    registry = ToolRegistry(wiki=DummyWiki(), memory=DummyMemory(), skills=DummySkills())

    result = registry.get("wiki_read").handler(None, {"page_key": "index"})

    assert result["body"] == "Body"
    assert result["outlinks"] == []


def test_wiki_follow_links_tool_returns_neighbors() -> None:
    registry = ToolRegistry(wiki=DummyWiki(), memory=DummyMemory(), skills=DummySkills())

    result = registry.get("wiki_follow_links").handler(None, {"page_key": "index"})

    assert result["items"][0]["dst_page_key"] == "other"


def test_skill_search_and_read_tools_are_progressive() -> None:
    registry = ToolRegistry(wiki=DummyWiki(), memory=DummyMemory(), skills=DummySkills())

    search = registry.get("skill_search").handler(None, {"query": "debug"})
    read = registry.get("skill_read").handler(None, {"skill_key": "coding/debug"})

    assert search["items"][0]["skill_key"] == "coding/debug"
    assert read["files"][0]["file_path"] == "SKILL.md"


def test_tool_search_bridge_finds_tools_without_full_schema() -> None:
    registry = ToolRegistry(wiki=DummyWiki(), memory=DummyMemory(), skills=DummySkills(), max_direct_tool_schemas=1)

    tools = registry.openai_tools()
    result = registry.get("tool_search").handler(None, {"query": "wiki"})

    assert {item["function"]["name"] for item in tools} <= {
        "tool_search",
        "tool_describe",
        "tool_call",
        "wiki_orient",
        "wiki_search",
        "wiki_read",
        "memory_search",
        "skill_search",
        "skill_read",
        "wiki_sufficiency_check",
    }
    assert any(item["name"] == "wiki_search" for item in result["items"])


def test_wiki_sufficiency_requires_read_pages() -> None:
    registry = ToolRegistry(wiki=DummyWiki(), memory=DummyMemory(), skills=DummySkills())

    result = registry.get("wiki_sufficiency_check").handler(None, {"claim": "x", "read_pages": []})

    assert result["sufficient"] is False


def test_platform_service_validates_cron_expr() -> None:
    service = PlatformService()

    with pytest.raises(ValueError):
        service.upsert_cron_job(FakeDB(), name="bad", cron_expr="not cron")


def test_platform_service_validates_mcp_transport_requirements() -> None:
    service = PlatformService()

    with pytest.raises(ValueError):
        service.upsert_mcp_server(FakeDB(), name="remote", transport="sse")


def test_platform_imports_mcp_seed_from_yaml(tmp_path: Path) -> None:
    seed = tmp_path / "mcp_servers.yaml"
    seed.write_text(
        """
servers:
  search:
    command: "npx"
    args: ["-y", "open-websearch@latest"]
    tools:
      override_permission:
        search: safe
""",
        encoding="utf-8",
    )
    settings = type("Settings", (), {"mcp_config_file": seed})()
    db = FakeDB()
    service = PlatformService()

    result = service.import_mcp_seed(db, settings)

    server = next(item for item in db.objects if item.__class__.__name__ == "MCPServer")
    assert result["imported"] == 1
    assert server.name == "search"
    assert server.config_source.startswith("yaml:")
    assert server.permission_policy["override_permission"]["search"] == "safe"


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


def test_tool_executor_audits_approved_execution() -> None:
    registry = ToolRegistry(wiki=DummyWiki(), memory=DummyMemory(), skills=DummySkills())
    registry.register(
        ToolDefinition(
            name="gated",
            description="gated",
            scope="external.write",
            requires_approval=True,
            handler=lambda db, args: {"ok": True},
        )
    )
    events = DummyEvents()
    db = FakeDB()
    executor = ToolExecutor(registry, events=events, platform=PlatformService())

    result = executor.execute(db, tool_name="gated", arguments={"value": 1}, approved=True)

    assert result["status"] == "succeeded"
    assert any(item[0] == "tool.approved_execution" for item in events.audits)
