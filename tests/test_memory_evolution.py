from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.domain.memory import MemoryService
from backend.infra.models import Base, EvolutionProposal, Memory, MemoryHistory


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
        del statement
        return None

    def scalars(self, statement):
        del statement
        return type("Rows", (), {"all": lambda self_: []})()


class FakeWorkspace:
    def __init__(self) -> None:
        self.content = "# MEMORY\n"

    def read(self, kind: str):
        assert kind == "memory"
        return type("WorkspaceFile", (), {"content": self.content, "path": "memory/MEMORY.md"})()

    def write(self, kind: str, content: str, *, actor: str = "admin"):
        del actor
        assert kind == "memory"
        self.content = content
        return type("WorkspaceFile", (), {"content": content, "path": "memory/MEMORY.md", "updated_at": None})()


def _proposal(action: str, payload: dict) -> EvolutionProposal:
    item = EvolutionProposal(target_type="memory", action=action, status="pending", payload=payload)
    item.id = uuid.uuid4()
    return item


def _memory(content: str = "old fact") -> Memory:
    item = Memory(
        kind="agent_note",
        content=content,
        source="test",
        pinned=False,
        archived=False,
        importance=0.5,
        confidence=0.5,
        stability=0.5,
        metadata_json={},
    )
    item.id = uuid.uuid4()
    item.created_at = datetime.now(UTC)
    item.updated_at = datetime.now(UTC)
    return item


def test_memory_proposal_create_is_applyable() -> None:
    db = FakeDB()
    proposal = _proposal("create", {"kind": "semantic", "content": "lesson", "source": "dream"})
    db.add(proposal)
    service = MemoryService(events=None, workspace=FakeWorkspace())

    applied = service.apply_proposal(db, proposal.id)

    assert applied.status == "applied"
    assert applied.result["action"] == "create"
    assert any(isinstance(item, Memory) and item.content == "lesson" for item in db.objects)


def test_memory_proposal_archive_is_applyable() -> None:
    db = FakeDB()
    memory = _memory()
    proposal = _proposal("archive", {"memory_id": str(memory.id)})
    db.add(memory)
    db.add(proposal)
    service = MemoryService(events=None, workspace=FakeWorkspace())

    applied = service.apply_proposal(db, proposal.id)

    assert applied.status == "applied"
    assert memory.archived is True


def test_memory_file_sync_updates_marked_entries() -> None:
    db = FakeDB()
    workspace = FakeWorkspace()
    service = MemoryService(events=None, workspace=workspace)
    memory = service.create(db, kind="agent_note", content="old lesson", source="test")
    workspace.content = workspace.content.replace("old lesson", "edited lesson").replace("confidence=0.50", "confidence=0.80")

    result = service.sync_from_memory_file(db)

    assert result["updated"] == 1
    assert memory.content == "edited lesson"
    assert memory.confidence == 0.8


def test_memory_sqlite_fts_scores_and_filters_expired(tmp_path) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    workspace = FakeWorkspace()
    service = MemoryService(events=None, workspace=workspace)

    with Session(engine) as db:
        service.create(db, kind="semantic", content="Orchid retrieval should use BM25 memory search.", source="test", importance=0.9)
        service.create(
            db,
            kind="semantic",
            content="Expired orchid fact should not be retrieved.",
            source="test",
            importance=1.0,
            metadata={"valid_to": "2000-01-01T00:00:00+00:00"},
        )
        db.commit()
        service.refresh_fts(db)
        result = service.search(db, "orchid retrieval", limit=5)
        governance = service.governance_status(db, budget_chars=10)

    assert result
    assert result[0]["source"] in {"memory_fts", "memory_index"}
    assert "Expired orchid" not in " ".join(item["memory"].content for item in result)
    assert governance["budget"]["over_budget"] is True


def test_memory_restore_from_history() -> None:
    db = FakeDB()
    workspace = FakeWorkspace()
    service = MemoryService(events=None, workspace=workspace)
    memory = service.create(db, kind="agent_note", content="restore me", source="test")
    history = next(item for item in db.objects if isinstance(item, MemoryHistory) and item.action == "create")
    memory.content = "changed"

    restored = service.restore(db, history.id)

    assert restored.content == "restore me"
