from __future__ import annotations

import uuid
from datetime import UTC, datetime

from backend.runtime.dream import DreamRuntime


class DummyEvents:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event_type, payload=None, **kwargs) -> None:
        self.events.append((event_type, payload or {}, kwargs))


class DummySkills:
    def __init__(self) -> None:
        self.proposals = []

    def create_proposal(self, db, **kwargs):
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
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.turn_id = "turn-2"
        self.tool_name = "wiki_compile"
        self.arguments = {}
        self.result = {"error": "broken wikilink"}
        self.started_at = datetime.now(UTC)


def test_dream_review_creates_skill_proposal_from_error_evidence() -> None:
    skills = DummySkills()
    runtime = DreamRuntime(skills=skills, events=DummyEvents())
    db = FakeDB(memories=[DummyMemory("Tool approval failed during wiki compile")], runs=[DummyRun()])

    result = runtime.run_review(db)

    assert result.proposals_created == 6
    assert skills.proposals[0].target_type == "skill"
    assert skills.proposals[1].target_type == "memory"
    assert skills.proposals[2].target_type == "wiki"
    assert skills.proposals[0].payload["required_checks"][-1] == "human_review_before_apply"
    assert "SKILL.md" in skills.proposals[0].payload["files"]


def test_dream_review_skips_existing_group() -> None:
    skills = DummySkills()
    runtime = DreamRuntime(skills=skills, events=DummyEvents())
    db = FakeDB(memories=[DummyMemory("same repeated failure")], proposal_exists=True)

    result = runtime.run_review(db)

    assert result.proposals_created == 0
    assert skills.proposals == []
