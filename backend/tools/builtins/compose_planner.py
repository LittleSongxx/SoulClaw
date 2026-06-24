from __future__ import annotations

import json
from difflib import SequenceMatcher
from typing import Any, Optional

from ..base import Tool, ToolPermission, ToolResult


class ComposePlannerTool(Tool):
    name = "compose_planner"
    description = (
        "Build a reviewable multi-step execution plan from available skills,"
        " core tools, and MCP tools. This does not execute side effects; it"
        " returns JSON and can optionally create a workflow proposal."
    )
    permission = ToolPermission.SAFE
    is_read_only = True
    is_concurrency_safe = True
    is_destructive = False
    always_load = True
    search_hint = "compose plan workflow multi-step skills tools MCP recipe"
    parameters_schema = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "User goal to plan for.",
            },
            "create_proposal": {
                "type": "boolean",
                "default": False,
                "description": "When true, enqueue the generated plan as a workflow proposal.",
            },
            "max_steps": {
                "type": "integer",
                "minimum": 2,
                "maximum": 12,
                "default": 6,
            },
        },
        "required": ["goal"],
    }

    def __init__(
        self,
        *,
        registry: Any,
        skill_loader: Any,
        proposal_store: Optional[Any] = None,
    ) -> None:
        self._registry = registry
        self._skill_loader = skill_loader
        self._proposal_store = proposal_store

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        goal = str(arguments.get("goal") or "").strip()
        if not goal:
            return ToolResult(ok=False, content="", error="goal is required")
        try:
            max_steps = int(arguments.get("max_steps") or 6)
        except (TypeError, ValueError):
            max_steps = 6
        max_steps = max(2, min(12, max_steps))

        skills = self._rank_skills(goal)[:5]
        tools = [
            t for t in self._registry.search(goal, limit=12, include_description=True)
            if t["name"] != self.name
        ][:8]
        steps = self._build_steps(goal, skills, tools, max_steps=max_steps)
        plan = {
            "goal": goal,
            "selected_skills": skills,
            "candidate_tools": tools,
            "steps": steps,
            "confirm_points": [
                s for s in steps
                if s.get("requires_confirmation") or s.get("risk_level") in {"high", "critical"}
            ],
            "risk_level": self._overall_risk(steps),
            "notes": [
                "Plan only; execution still goes through ToolLoopRunner permissions and guardrails.",
                "Mutating skill/wiki/workflow changes should be applied via review proposals.",
            ],
        }
        proposal_id = None
        if bool(arguments.get("create_proposal")) and self._proposal_store is not None:
            proposal = self._proposal_store.create(
                target_type="workflow",
                action="compose_recipe",
                payload={
                    "name": self._recipe_name(goal),
                    "goal": goal,
                    "plan": plan,
                },
                evidence={"source": "compose_planner", "goal": goal},
                confidence=0.7,
                risk_level=plan["risk_level"],
                source="compose_planner",
            )
            proposal_id = proposal["id"]
            plan["proposal_id"] = proposal_id
        return ToolResult(
            ok=True,
            content=json.dumps(plan, ensure_ascii=False, indent=2),
            raw={"plan": plan, "proposal_id": proposal_id},
        )

    def _rank_skills(self, goal: str) -> list[dict[str, Any]]:
        if self._skill_loader is None:
            return []
        try:
            manifests = self._skill_loader.list()
        except Exception:
            manifests = []
        query = goal.lower()
        out: list[tuple[float, dict[str, Any]]] = []
        for manifest in manifests:
            fields: list[str] = [
                getattr(manifest, "id", ""),
                getattr(manifest, "name", ""),
                getattr(manifest, "description", ""),
            ]
            fields.extend(getattr(manifest, "tags", []) or [])
            fields.extend(getattr(manifest, "triggers", []) or [])
            fields.extend(getattr(manifest, "capabilities", []) or [])
            fields.extend(getattr(manifest, "inputs", []) or [])
            fields.extend(getattr(manifest, "outputs", []) or [])
            text = " ".join(str(f) for f in fields if f).lower()
            if not text:
                continue
            score = 0.0
            if query in text:
                score += 4.0
            q_terms = {t for t in query.replace("_", " ").split() if len(t) >= 2}
            t_terms = {t for t in text.replace("_", " ").split() if len(t) >= 2}
            if q_terms:
                score += 3.0 * (len(q_terms & t_terms) / len(q_terms))
            ratio = SequenceMatcher(None, query, text[:500]).ratio()
            score += ratio
            if score <= 0.1:
                continue
            out.append((score, {
                "skill_id": getattr(manifest, "id", ""),
                "name": getattr(manifest, "name", ""),
                "description": getattr(manifest, "description", ""),
                "capabilities": list(getattr(manifest, "capabilities", []) or []),
                "required_tools": list(getattr(manifest, "required_tools", []) or []),
                "approval_level": getattr(manifest, "approval_level", "review"),
                "score": round(score, 3),
            }))
        out.sort(key=lambda item: (-item[0], item[1]["skill_id"]))
        return [item for _, item in out]

    @staticmethod
    def _build_steps(
        goal: str,
        skills: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_steps: int,
    ) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []
        if skills:
            steps.append({
                "id": "skill_context",
                "kind": "skill",
                "name": skills[0]["skill_id"],
                "purpose": "Load the most relevant procedural context for the goal.",
                "depends_on": [],
                "risk_level": "low",
                "requires_confirmation": False,
            })
        for idx, tool in enumerate(tools, start=1):
            if len(steps) >= max_steps - 1:
                break
            permission = str(tool.get("permission") or "safe")
            destructive = bool(tool.get("is_destructive"))
            risk = "high" if destructive else ("medium" if permission == "confirm" else "low")
            steps.append({
                "id": f"tool_{idx}",
                "kind": "tool",
                "name": tool["name"],
                "purpose": tool.get("description") or f"Use {tool['name']} for part of the goal.",
                "depends_on": [steps[-1]["id"]] if steps else [],
                "risk_level": risk,
                "requires_confirmation": permission == "confirm" or destructive,
                "parallel_group": "read_only" if tool.get("is_read_only") and tool.get("is_concurrency_safe") else "",
            })
        steps.append({
            "id": "final_answer",
            "kind": "assistant",
            "name": "synthesize",
            "purpose": f"Answer the user goal with grounded results: {goal[:120]}",
            "depends_on": [s["id"] for s in steps[-2:]] if steps else [],
            "risk_level": "low",
            "requires_confirmation": False,
        })
        return steps

    @staticmethod
    def _overall_risk(steps: list[dict[str, Any]]) -> str:
        levels = [str(s.get("risk_level") or "low") for s in steps]
        if "critical" in levels:
            return "critical"
        if "high" in levels:
            return "high"
        if "medium" in levels:
            return "medium"
        return "low"

    @staticmethod
    def _recipe_name(goal: str) -> str:
        base = "".join(ch.lower() if ch.isalnum() else "-" for ch in goal)[:80]
        return "-".join(part for part in base.split("-") if part) or "compose-recipe"
