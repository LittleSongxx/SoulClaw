from __future__ import annotations

from backend.domain.skills import SkillService


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

