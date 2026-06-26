"""Tool registry, safety floor, and auditable execution for ZLAgent."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from backend.domain.memory import MemoryService
from backend.domain.platform import PlatformService
from backend.domain.skills import SkillService
from backend.domain.wiki import WikiService
from backend.infra.events import RuntimeEventBus
from backend.infra.models import ToolRun

if TYPE_CHECKING:
    from backend.runtime.gateway import GatewayRuntimeManager

ToolHandler = Callable[[Session, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    scope: str
    requires_approval: bool = False
    available: bool = True
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "type": "object",
        "properties": {},
        "additionalProperties": True,
    })
    handler: ToolHandler | None = field(default=None, repr=False, compare=False)

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolSafetyFloor:
    """Hard safety floor inspired by hermes-agent/mateclaw approval patterns."""

    blocked_patterns = (
        re.compile(r"\brm\s+-rf\s+/", re.IGNORECASE),
        re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE),
        re.compile(r"\bgit\s+checkout\s+--\s+/", re.IGNORECASE),
        re.compile(r"\bmkfs\.", re.IGNORECASE),
        re.compile(r"\bchmod\s+-R\s+777\s+/", re.IGNORECASE),
    )

    mutating_scopes = {"memory.write", "skill.write", "wiki.write", "system.write", "external.write"}

    def validate(self, tool: ToolDefinition, arguments: dict[str, Any], *, approved: bool = False) -> None:
        serialized = repr(arguments)
        for pattern in self.blocked_patterns:
            if pattern.search(serialized):
                raise PermissionError(f"blocked by hard safety floor: {pattern.pattern}")
        if tool.scope in self.mutating_scopes and tool.requires_approval and not approved:
            raise PermissionError("tool requires approval before execution")


class ToolRegistry:
    def __init__(
        self,
        *,
        wiki: WikiService,
        memory: MemoryService,
        skills: SkillService,
        events: RuntimeEventBus | None = None,
        gateway: GatewayRuntimeManager | None = None,
    ) -> None:
        self.wiki = wiki
        self.memory = memory
        self.skills = skills
        self.events = events
        self.gateway = gateway
        self._tools: dict[str, ToolDefinition] = {}
        self._register_builtin_tools()
        if self.gateway is not None:
            self.install_gateway(self.gateway)

    def list(self) -> list[ToolDefinition]:
        return sorted(self._tools.values(), key=lambda item: item.name)

    def openai_tools(self) -> list[dict[str, Any]]:
        return [tool.openai_schema() for tool in self.list() if tool.available]

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def register(self, definition: ToolDefinition) -> None:
        self._tools[definition.name] = definition
        if self.events:
            self.events.emit("tool.registered", {"tool_name": definition.name, "scope": definition.scope})

    def unregister_prefix(self, prefix: str) -> int:
        names = [name for name in self._tools if name.startswith(prefix)]
        for name in names:
            del self._tools[name]
            if self.events:
                self.events.emit("tool.unregistered", {"tool_name": name})
        return len(names)

    def register_many(self, definitions: list[ToolDefinition]) -> None:
        for definition in definitions:
            self.register(definition)

    def _register_builtin_tools(self) -> None:
        self.register(
            ToolDefinition(
                name="wiki_orient",
                description="Read LLM-Wiki schema, index, recent log, and page map before retrieval.",
                scope="wiki.read",
                handler=self._wiki_orient,
            )
        )
        self.register(
            ToolDefinition(
                name="wiki_search",
                description="Search the Wiki page index. Use wiki_read before relying on a page as evidence.",
                scope="wiki.read",
                handler=self._wiki_search,
            )
        )
        self.register(
            ToolDefinition(
                name="wiki_read",
                description="Read a compiled Wiki page by page_key.",
                scope="wiki.read",
                handler=self._wiki_read,
            )
        )
        self.register(
            ToolDefinition(
                name="wiki_follow_links",
                description="Traverse resolved Wiki links from or to a page_key.",
                scope="wiki.read",
                handler=self._wiki_follow_links,
                parameters={
                    "type": "object",
                    "required": ["page_key"],
                    "properties": {
                        "page_key": {"type": "string"},
                        "direction": {"type": "string", "enum": ["out", "in", "both"]},
                        "limit": {"type": "integer"},
                    },
                },
            )
        )
        self.register(
            ToolDefinition(
                name="wiki_lint",
                description="Lint LLM-Wiki structure and write Error Book entries.",
                scope="wiki.write",
                requires_approval=False,
                handler=self._wiki_lint,
            )
        )
        self.register(
            ToolDefinition(
                name="wiki_compile",
                description="Compile Markdown Wiki source into the local page/link/error-book index.",
                scope="wiki.write",
                requires_approval=False,
                handler=self._wiki_compile,
            )
        )
        self.register(
            ToolDefinition(
                name="memory_search",
                description="Search long-term memory index and return readable memory references.",
                scope="memory.read",
                handler=self._memory_search,
            )
        )
        self.register(
            ToolDefinition(
                name="memory_get",
                description="Read one long-term memory item by id.",
                scope="memory.read",
                handler=self._memory_get,
                parameters={
                    "type": "object",
                    "required": ["memory_id"],
                    "properties": {"memory_id": {"type": "string"}},
                },
            )
        )
        self.register(
            ToolDefinition(
                name="memory_create",
                description="Create an L2/L3 memory item.",
                scope="memory.write",
                requires_approval=False,
                handler=self._memory_create,
            )
        )
        self.register(
            ToolDefinition(
                name="skills_scan",
                description="Scan workspace skills and rebuild skill indexes.",
                scope="skill.read",
                handler=self._skills_scan,
            )
        )
        self.register(
            ToolDefinition(
                name="skill_test",
                description="Run manifest lint/static safety tests for a skill.",
                scope="skill.read",
                handler=self._skill_test,
            )
        )
    def install_gateway(self, gateway: GatewayRuntimeManager) -> None:
        self.gateway = gateway
        self.register(
            ToolDefinition(
                name="gateway_send",
                description="Send a message through an enabled gateway connection.",
                scope="external.write",
                requires_approval=True,
                parameters={
                    "type": "object",
                    "required": ["gateway_name", "target_id", "text"],
                    "properties": {
                        "gateway_name": {"type": "string"},
                        "target_id": {"type": "string"},
                        "text": {"type": "string"},
                        "metadata": {"type": "object"},
                    },
                },
                handler=self._gateway_send,
            )
        )

    def _wiki_orient(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.wiki.orientation(db, recent_log_lines=int(arguments.get("recent_log_lines") or 40))

    def _wiki_search(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "")
        limit = int(arguments.get("limit") or 10)
        return {
            "items": [
                {
                    "page_key": item["page_key"],
                    "title": item["title"],
                    "path": item["path"],
                    "summary": item["summary"],
                    "tags": item.get("tags", []),
                    "page_type": item.get("page_type", ""),
                    "confidence": item.get("confidence", 0.5),
                    "source": item["source"],
                    "score": item["score"],
                }
                for item in self.wiki.search(db, query, limit=limit)
            ]
        }

    def _wiki_read(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        page_key = str(arguments.get("page_key") or "")
        page = self.wiki.read(db, page_key)
        if page is None:
            raise KeyError(f"wiki page not found: {page_key}")
        graph = self.wiki.read_with_graph(db, page_key)
        return {
            "page_key": page.page_key,
            "title": page.title,
            "summary": page.summary,
            "body": page.body,
            "metadata": page.metadata_json or {},
            "outlinks": graph["outlinks"] if graph else [],
            "backlinks": graph["backlinks"] if graph else [],
        }

    def _wiki_follow_links(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "items": self.wiki.follow_links(
                db,
                str(arguments["page_key"]),
                direction=str(arguments.get("direction") or "out"),
                limit=int(arguments.get("limit") or 50),
            )
        }

    def _wiki_compile(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.wiki.compile(db)

    def _wiki_lint(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        del arguments
        return self.wiki.lint(db)

    def _memory_search(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "")
        limit = int(arguments.get("limit") or 10)
        return {
            "items": [
                {
                    "id": str(item["memory"].id),
                    "kind": item["memory"].kind,
                    "source": item["source"],
                    "score": item["score"],
                    "summary": item["memory"].content[:500],
                }
                for item in self.memory.search(db, query, limit=limit)
            ]
        }

    def _memory_get(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        memory_id = uuid.UUID(str(arguments.get("memory_id") or ""))
        memory = self.memory.get(db, memory_id)
        if memory is None:
            raise KeyError(f"memory not found: {memory_id}")
        return {
            "id": str(memory.id),
            "kind": memory.kind,
            "content": memory.content,
            "source": memory.source,
            "pinned": memory.pinned,
            "importance": memory.importance,
            "confidence": memory.confidence,
            "stability": memory.stability,
            "metadata": memory.metadata_json or {},
        }

    def _memory_create(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        memory = self.memory.create(
            db,
            kind=str(arguments.get("kind") or "agent_note"),
            content=str(arguments["content"]),
            source=str(arguments.get("source") or "tool"),
            pinned=bool(arguments.get("pinned", False)),
            importance=float(arguments.get("importance", 0.5)),
            confidence=float(arguments.get("confidence", 0.5)),
            stability=float(arguments.get("stability", 0.5)),
            metadata=arguments.get("metadata") if isinstance(arguments.get("metadata"), dict) else {},
        )
        return {"id": str(memory.id), "kind": memory.kind, "content": memory.content}

    def _skills_scan(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.skills.scan(db)

    def _skill_test(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.skills.test(db, str(arguments["skill_key"]))

    def _gateway_send(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        del db
        if self.gateway is None:
            raise RuntimeError("gateway runtime is not configured")
        from backend.runtime.gateway import OutboundGatewayMessage

        metadata = arguments.get("metadata") if isinstance(arguments.get("metadata"), dict) else {}
        return self.gateway.send(
            OutboundGatewayMessage(
                gateway_name=str(arguments["gateway_name"]),
                target_id=str(arguments["target_id"]),
                text=str(arguments["text"]),
                metadata=metadata,
            )
        )


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        events: RuntimeEventBus | None = None,
        safety: ToolSafetyFloor | None = None,
        platform: PlatformService | None = None,
    ) -> None:
        self.registry = registry
        self.events = events
        self.safety = safety or ToolSafetyFloor()
        self.platform = platform

    def execute(
        self,
        db: Session,
        *,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        turn_id: str = "",
        approved: bool = False,
    ) -> dict[str, Any]:
        arguments = arguments or {}
        definition = self.registry.get(tool_name)
        if definition is None or definition.handler is None:
            raise KeyError(f"tool not found: {tool_name}")
        if not definition.available:
            raise RuntimeError(f"tool unavailable: {tool_name}")
        if definition.requires_approval and not approved:
            approval = None
            if self.platform is not None:
                approval = self.platform.create_approval(
                    db,
                    subject_type="tool_run",
                    subject_id=tool_name,
                    payload={"tool_name": tool_name, "arguments": arguments, "turn_id": turn_id},
                )
            if self.events:
                self.events.emit(
                    "tool.approval_required",
                    {
                        "tool_name": tool_name,
                        "approval_id": str(approval.id) if approval is not None else None,
                    },
                    severity="warning",
                    turn_id=turn_id,
                )
            raise PermissionError(f"tool requires approval: {tool_name}")
        if approved and self.events:
            self.events.audit(
                "tool.approved_execution",
                "tool",
                target_id=tool_name,
                payload={"tool_name": tool_name, "arguments": arguments, "turn_id": turn_id},
            )
            self.events.emit("tool.approved_execution", {"tool_name": tool_name}, turn_id=turn_id)
        try:
            self.safety.validate(definition, arguments, approved=approved)
        except PermissionError as exc:
            if self.events:
                self.events.emit(
                    "tool.blocked",
                    {"tool_name": tool_name, "error": str(exc)},
                    severity="warning",
                    turn_id=turn_id,
                )
            raise

        run = ToolRun(
            turn_id=turn_id,
            tool_name=tool_name,
            status="running",
            arguments=arguments,
            result={"checkpoint": str(uuid.uuid4())},
        )
        db.add(run)
        db.flush()
        if self.events:
            self.events.emit("tool.started", {"tool_run_id": str(run.id), "tool_name": tool_name}, turn_id=turn_id)
        try:
            result = definition.handler(db, arguments)
        except Exception as exc:
            run.status = "failed"
            run.result = {"error": str(exc)}
            run.finished_at = datetime.now(UTC)
            if self.events:
                self.events.emit(
                    "tool.failed",
                    {"tool_run_id": str(run.id), "tool_name": tool_name, "error": str(exc)},
                    severity="warning",
                    turn_id=turn_id,
                )
            raise
        run.status = "succeeded"
        run.result = result
        run.finished_at = datetime.now(UTC)
        if self.events:
            self.events.emit("tool.succeeded", {"tool_run_id": str(run.id), "tool_name": tool_name}, turn_id=turn_id)
        return {"tool_run_id": str(run.id), "tool_name": tool_name, "status": run.status, "result": result}
