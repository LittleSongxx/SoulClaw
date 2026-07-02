"""Unified policy engine for tool and runtime operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.infra.events import RuntimeEventBus
from backend.infra.models import PolicyRule


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    requires_approval: bool
    risk_level: str
    reason: str
    rule_id: str = ""
    subject: str = ""
    scope: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "risk_level": self.risk_level,
            "reason": self.reason,
            "rule_id": self.rule_id,
            "subject": self.subject,
            "scope": self.scope,
        }


DEFAULT_POLICY_RULES: tuple[dict[str, Any], ...] = (
    {
        "rule_id": "tool-skill-use-trace-allow",
        "subject": "tool:skill_use_trace",
        "scope": "memory.write",
        "action": "allow",
        "risk_level": "low",
        "requires_approval": False,
        "config": {"forced_kind": "skill_trace", "max_outcome_chars": 240, "max_notes_chars": 1000},
    },
    {
        "rule_id": "scope-read-allow",
        "subject": "scope:*.read",
        "scope": "*.read",
        "action": "allow",
        "risk_level": "low",
        "requires_approval": False,
        "config": {},
    },
    {
        "rule_id": "scope-memory-write-approval",
        "subject": "scope:memory.write",
        "scope": "memory.write",
        "action": "allow",
        "risk_level": "medium",
        "requires_approval": True,
        "config": {"reason": "long-term memory writes must be human approved unless a narrower rule allows them"},
    },
    {
        "rule_id": "scope-external-write-approval",
        "subject": "scope:external.write",
        "scope": "external.write",
        "action": "allow",
        "risk_level": "high",
        "requires_approval": True,
        "config": {"reason": "external side effects require approval"},
    },
    {
        "rule_id": "scope-skill-write-approval",
        "subject": "scope:skill.write",
        "scope": "skill.write",
        "action": "allow",
        "risk_level": "high",
        "requires_approval": True,
        "config": {"reason": "skill file/index writes require approval"},
    },
    {
        "rule_id": "scope-wiki-write-approval",
        "subject": "scope:wiki.write",
        "scope": "wiki.write",
        "action": "allow",
        "risk_level": "medium",
        "requires_approval": True,
        "config": {"reason": "wiki writes require approval"},
    },
    {
        "rule_id": "scope-system-write-approval",
        "subject": "scope:system.write",
        "scope": "system.write",
        "action": "allow",
        "risk_level": "critical",
        "requires_approval": True,
        "config": {"reason": "system writes require approval"},
    },
)


class ToolPolicyEngine:
    """Small database-backed rule engine with deterministic defaults."""

    def __init__(self, *, events: RuntimeEventBus | None = None) -> None:
        self.events = events

    def ensure_defaults(self, db: Session) -> int:
        existing = {rule.rule_id: rule for rule in db.scalars(select(PolicyRule)).all()}
        created = 0
        for spec in DEFAULT_POLICY_RULES:
            rule = existing.get(str(spec["rule_id"]))
            if rule is None:
                rule = PolicyRule(**spec)
                db.add(rule)
                created += 1
                continue
            # Keep user-edited action/risk/approval intact, but refresh static labels
            # when an older default row is missing them.
            rule.subject = rule.subject or str(spec["subject"])
            rule.scope = rule.scope or str(spec["scope"])
            if not isinstance(rule.config, dict) or not rule.config:
                rule.config = dict(spec.get("config") or {})
        if created and self.events:
            self.events.emit("policy.defaults.seeded", {"created": created})
        return created

    def list_rules(self, db: Session) -> list[PolicyRule]:
        self.ensure_defaults(db)
        return list(db.scalars(select(PolicyRule).order_by(PolicyRule.subject, PolicyRule.rule_id)).all())

    def update_rule(self, db: Session, rule_id: str, payload: dict[str, Any]) -> PolicyRule:
        self.ensure_defaults(db)
        rule = db.scalar(select(PolicyRule).where(PolicyRule.rule_id == rule_id))
        if rule is None:
            raise KeyError(f"policy rule not found: {rule_id}")
        for field in ("action", "risk_level", "requires_approval", "enabled"):
            if field in payload:
                setattr(rule, field, payload[field])
        if "config" in payload and isinstance(payload["config"], dict):
            rule.config = payload["config"]
        rule.updated_at = datetime.now(UTC)
        if self.events:
            self.events.audit("policy.rule.update", "policy_rule", target_id=rule.rule_id, payload=payload)
            self.events.emit("policy.rule.updated", {"rule_id": rule.rule_id})
        return rule

    def status(self, db: Session) -> dict[str, Any]:
        rules = self.list_rules(db)
        return {
            "enabled": True,
            "rules": len(rules),
            "approval_required": sum(1 for rule in rules if rule.enabled and rule.requires_approval),
            "denied": sum(1 for rule in rules if rule.enabled and rule.action == "deny"),
            "risk_matrix": self.risk_matrix(db),
        }

    def risk_matrix(self, db: Session) -> list[dict[str, Any]]:
        items = []
        for rule in self.list_rules(db):
            items.append(
                {
                    "rule_id": rule.rule_id,
                    "subject": rule.subject,
                    "scope": rule.scope,
                    "action": rule.action,
                    "risk_level": rule.risk_level,
                    "requires_approval": rule.requires_approval,
                    "enabled": rule.enabled,
                }
            )
        return items

    def decide(
        self,
        db: Session,
        *,
        tool_name: str,
        scope: str,
        arguments: dict[str, Any] | None = None,
        declared_requires_approval: bool = False,
    ) -> PolicyDecision:
        del arguments
        self.ensure_defaults(db)
        rule = self._matching_rule(db, tool_name=tool_name, scope=scope)
        if rule is not None:
            allowed = rule.action != "deny"
            reason = f"matched policy rule {rule.rule_id}"
            return PolicyDecision(
                allowed=allowed,
                requires_approval=bool(rule.requires_approval or declared_requires_approval),
                risk_level=rule.risk_level,
                reason=reason,
                rule_id=rule.rule_id,
                subject=rule.subject,
                scope=scope,
            )
        if scope.endswith(".read") or scope in {"safe", "read"}:
            return PolicyDecision(True, declared_requires_approval, "low", "safe/read scope", scope=scope)
        if scope.endswith(".write") or scope.startswith("external.") or scope.startswith("system."):
            return PolicyDecision(True, True, "high", "mutating scope default approval", scope=scope)
        return PolicyDecision(True, declared_requires_approval, "medium", "fallback allow", scope=scope)

    def _matching_rule(self, db: Session, *, tool_name: str, scope: str) -> PolicyRule | None:
        for subject in (f"tool:{tool_name}", f"scope:{scope}", "scope:*.read" if scope.endswith(".read") else ""):
            if not subject:
                continue
            rule = db.scalar(
                select(PolicyRule)
                .where(PolicyRule.enabled.is_(True), PolicyRule.subject == subject)
                .order_by(PolicyRule.rule_id)
            )
            if rule is not None:
                return rule
        return None
