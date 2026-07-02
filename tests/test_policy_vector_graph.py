from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.domain.memory import MemoryService
from backend.domain.policy import ToolPolicyEngine
from backend.domain.runs import AgentRunService
from backend.domain.tools import ToolApprovalRequired, ToolDefinition, ToolExecutor, ToolRegistry
from backend.domain.vector import KnowledgeVectorService
from backend.infra.config import Settings
from backend.infra.models import Base, Memory
from backend.runtime.agent import AgentRuntime


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


class DummyEvents:
    def __init__(self) -> None:
        self.events = []
        self.audits = []

    def emit(self, event_type, payload=None, **kwargs) -> None:
        self.events.append((event_type, payload or {}, kwargs))

    def audit(self, action, target_type, **kwargs) -> None:
        self.audits.append((action, target_type, kwargs))


class DummyWiki:
    def orientation(self, db):
        del db
        return {"pages": [], "page_count": 0, "schema": "", "index": "", "recent_log": ""}

    def search(self, db, message, limit=5):
        del db, message, limit
        return []


class DummyMemory:
    def resident_context(self, db, message, dynamic_limit=5):
        del db, message, dynamic_limit
        return {"resident": [], "dynamic": []}


class DummySkills:
    def search(self, db, query, *, limit=10):
        del db, query, limit
        return []

    def scan(self, db):
        del db
        return {"skills": 0}


class DummyPlatform:
    def create_approval(self, db, **kwargs):
        from backend.infra.models import Approval

        approval = Approval(
            subject_type=kwargs["subject_type"],
            subject_id=kwargs["subject_id"],
            payload=kwargs.get("payload") or {},
            original_tool_call=kwargs.get("original_tool_call") or {},
            turn_checkpoint=kwargs.get("turn_checkpoint") or {},
            allowed_decisions=kwargs.get("allowed_decisions") or [],
            resume_state=kwargs.get("resume_state") or {},
        )
        db.add(approval)
        db.flush()
        return approval


@dataclass
class FakeEmbeddingResponse:
    vectors: list[list[float]]
    model: str = "fake-embedding"
    dimensions: int = 3
    raw: dict | None = None


class FakeEmbeddings:
    configured = True

    def embed_texts(self, texts: list[str]) -> FakeEmbeddingResponse:
        vectors = []
        for text in texts:
            lowered = text.lower()
            vectors.append([
                1.0 if "alpha" in lowered else 0.0,
                1.0 if "beta" in lowered else 0.0,
                1.0 if "gamma" in lowered else 0.0,
            ])
        return FakeEmbeddingResponse(vectors=vectors, raw={})


def test_policy_requires_memory_create_approval(db) -> None:
    policy = ToolPolicyEngine(events=DummyEvents())
    registry = ToolRegistry(wiki=DummyWiki(), memory=DummyMemory(), skills=DummySkills())
    executor = ToolExecutor(registry, policy=policy, platform=DummyPlatform())

    with pytest.raises(ToolApprovalRequired):
        executor.execute(db, tool_name="memory_create", arguments={"content": "remember this"})

    approvals = [item for item in db.query(Base.registry._class_registry["Approval"]).all()]
    assert approvals
    assert approvals[0].payload["policy"]["requires_approval"] is True


def test_skill_use_trace_is_allowed_by_narrow_policy(db) -> None:
    policy = ToolPolicyEngine()
    decision = policy.decide(
        db,
        tool_name="skill_use_trace",
        scope="memory.write",
        arguments={"skill_key": "coding/test", "outcome": "helped"},
    )

    assert decision.allowed is True
    assert decision.requires_approval is False
    assert decision.rule_id == "tool-skill-use-trace-allow"


def test_vector_service_upserts_and_searches_memory(db) -> None:
    settings = Settings(vector_mode="required", embedding_model="fake-embedding", embedding_dimensions=3, embedding_batch_size=2)
    vector = KnowledgeVectorService(embeddings=FakeEmbeddings(), settings=settings)
    memory = Memory(kind="semantic", content="alpha project note", source="test")
    db.add(memory)
    db.flush()

    result = vector.upsert_memories(db, [memory])
    hits = vector.search(db, query="alpha", source_type="memory", limit=5)

    assert result["embedded"] == 1
    assert hits[0]["source_id"] == str(memory.id)
    assert hits[0]["chunk_key"].startswith("memory:")
    assert hits[0]["vector_score"] > 0.9


def test_memory_search_returns_hybrid_scores(db) -> None:
    settings = Settings(vector_mode="required", embedding_model="fake-embedding", embedding_dimensions=3)
    vector = KnowledgeVectorService(embeddings=FakeEmbeddings(), settings=settings)
    service = MemoryService(events=None, vector=vector)
    memory = service._create_applied_memory(db, kind="semantic", content="alpha durable note", source="test")

    hits = service.search(db, "alpha", limit=3)

    assert hits[0]["memory"].id == memory.id
    assert "hybrid_score" in hits[0]
    assert "vector_score" in hits[0]
    assert "chunk_key" in hits[0]


def test_agent_run_service_records_graph_for_turn(db) -> None:
    events = DummyEvents()
    runs = AgentRunService(settings=Settings(agent_engine="langgraph"), events=events)
    runtime = AgentRuntime(wiki=DummyWiki(), memory=DummyMemory(), events=events, runs=runs)

    result = runtime.run_turn(db, "hello", session_id="graph-test")
    run_id = result.context["run"]["run_id"]
    graph = runs.graph(db, run_id)

    assert result.status == "completed"
    assert graph["run"]["engine"] == "langgraph"
    assert [node["id"] for node in graph["nodes"]][:3] == ["record_user", "plan", "delegate_or_context"]
