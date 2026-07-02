from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.domain.evolution_proposals import EvolutionProposalService
from backend.domain.skills import SkillService
from backend.infra.models import Base


class FakeSettings:
    def __init__(self, root) -> None:
        self.resolved_skills_root = root


def test_skill_lint_requires_skill_md() -> None:
    result = SkillService().lint_files({"notes.md": "hello"})

    assert not result.ok
    assert "SKILL.md is required" in result.errors


def test_skill_lint_blocks_destructive_commands() -> None:
    result = SkillService().lint_files({"SKILL.md": "# Bad\n\nRun `git reset --hard`."})

    assert not result.ok
    assert any("forbidden unsafe pattern" in item for item in result.errors)


def test_skill_lint_accepts_manifest_tests() -> None:
    result = SkillService().lint_files(
        {
            "SKILL.md": """---
name: Research Helper
required_tools:
  - web_search
test_cases:
  - name: routes papers
    input: arxiv
---

# Research Helper
"""
        }
    )

    assert result.ok
    assert result.tests[0]["name"] == "routes papers"
    assert result.tests[0]["kind"] == "golden_prompt"


def test_skill_safe_test_cases_execute_without_shell(tmp_path) -> None:
    service = SkillService(settings=FakeSettings(tmp_path))
    files = {
        "SKILL.md": """---
name: Research Helper
required_tools:
  - web_search
test_cases:
  - name: manifest
    kind: lint
  - name: mentions route
    kind: golden_prompt
    expected_contains: route papers
  - name: required tools
    kind: mock_tool
    required_tools:
      - web_search
  - name: no destructive regression
    kind: regression
    must_not_contain:
      - git reset --hard
---

# Research Helper

Use this skill to route papers.
"""
    }

    result = service._test_files("research", files)

    assert result["ok"] is True
    assert {item["kind"] for item in result["tests"]} == {"lint", "golden_prompt", "mock_tool", "regression"}


def test_skill_proposal_stale_when_target_checksum_changes(tmp_path) -> None:
    skill_dir = tmp_path / "research"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Research\n\nOld body.", encoding="utf-8")
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    service = SkillService(settings=FakeSettings(tmp_path))
    proposals = EvolutionProposalService(skills=service)

    with Session(engine) as db:
        service.scan(db)
        proposal = proposals.create(
            db,
            target_type="skill",
            action="upsert",
            payload={
                "skill_key": "research",
                "files": {
                    "SKILL.md": """---
name: Research
test_cases:
  - name: manifest
    kind: lint
---

# Research

Updated body.
"""
                },
            },
        )
        (skill_dir / "SKILL.md").write_text("# Research\n\nChanged outside proposal.", encoding="utf-8")
        applied = service.apply_proposal(db, proposal.id)

    assert applied.status == "stale"
    assert applied.stale_reason


def test_skill_lint_rejects_oversized_support_surface() -> None:
    files = {"SKILL.md": "# Big"}
    for index in range(20):
        files[f"notes/{index}.md"] = "x"

    result = SkillService().lint_files(files)

    assert not result.ok
    assert any("too many skill files" in item for item in result.errors)
