from __future__ import annotations

import uuid
from datetime import UTC, datetime

from backend.runtime.dream import DreamRuntime


class DummyEvents:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event_type, payload=None, **kwargs) -> None:
        self.events.append((event_type, payload or {}, kwargs))


class DummyProposals:
    def __init__(self) -> None:
        self.proposals = []

    def create(self, db, **kwargs):
        del db
        proposal = type(
            "Proposal",
            (),
            {
                "id": uuid.uuid4(),
                **kwargs,
            },
        )()
        self.proposals.append(proposal)
        return proposal


class FakeScalars:
    def __init__(self, items) -> None:
        self.items = items

    def all(self):
        return self.items


class FakeDB:
    def __init__(self, memories=None, runs=None, proposal_exists: bool = False) -> None:
        self.memories = memories or []
        self.runs = runs or []
        self.proposal_exists = proposal_exists
        self.scalar_calls = 0

    def scalars(self, statement):
        text = str(statement)
        if "tool_runs" in text:
            return FakeScalars(self.runs)
        return FakeScalars(self.memories)

    def scalar(self, statement):
        del statement
        self.scalar_calls += 1
        return uuid.uuid4() if self.proposal_exists else None


class DummyMemory:
    def __init__(self, content: str) -> None:
        self.id = uuid.uuid4()
        self.kind = "error_signal"
        self.content = content
        self.importance = 0.8
        self.confidence = 0.7
        self.stability = 0.7
        self.source_turn_id = "turn-1"


class DummyRun:
    def __init__(self, error: str = "broken wikilink") -> None:
        self.id = uuid.uuid4()
        self.turn_id = "turn-2"
        self.tool_name = "wiki_compile"
        self.arguments = {}
        self.result = {"error": error}
        self.started_at = datetime.now(UTC)


def test_dream_review_creates_skill_proposal_from_error_evidence() -> None:
    proposals = DummyProposals()
    runtime = DreamRuntime(proposals=proposals, events=DummyEvents())
    db = FakeDB(
        memories=[DummyMemory("Tool approval failed during wiki compile")],
        runs=[DummyRun("Tool approval failed during wiki compile")],
    )

    result = runtime.run_review(db)

    assert result.proposals_created == 3
    assert result.learning_candidates == 1
    assert result.skill_candidates == 1
    assert proposals.proposals[0].target_type == "skill"
    assert proposals.proposals[1].target_type == "memory"
    assert proposals.proposals[2].target_type == "wiki"
    assert proposals.proposals[0].payload["learning_candidate"]["promote_to_skill"] is True
    assert proposals.proposals[0].payload["required_checks"][-1] == "human_review_before_apply"
    assert "SKILL.md" in proposals.proposals[0].payload["files"]


def test_dream_review_keeps_weak_signal_as_learning_candidate_not_skill() -> None:
    proposals = DummyProposals()
    runtime = DreamRuntime(proposals=proposals, events=DummyEvents())
    db = FakeDB(memories=[DummyMemory("single weak lesson")])

    result = runtime.run_review(db)

    assert result.proposals_created == 2
    assert result.learning_candidates == 1
    assert result.skill_candidates == 0
    assert result.skipped_skill_candidates == 1
    assert [proposal.target_type for proposal in proposals.proposals] == ["memory", "wiki"]
    assert proposals.proposals[0].payload["metadata"]["learning_candidate"]["promote_to_skill"] is False


def test_dream_review_skips_existing_group() -> None:
    proposals = DummyProposals()
    runtime = DreamRuntime(proposals=proposals, events=DummyEvents())
    db = FakeDB(memories=[DummyMemory("same repeated failure")], proposal_exists=True)

    result = runtime.run_review(db)

    assert result.proposals_created == 0
    assert proposals.proposals == []
