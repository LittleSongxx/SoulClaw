"""Tool registry, safety floor, and auditable execution for SoulClaw."""

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
    from backend.runtime.a2a import A2ARuntimeManager
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


class ToolApprovalRequired(PermissionError):
    """Raised when a tool call is paused behind an approval record."""

    def __init__(self, message: str, *, tool_name: str = "", approval_id: str = "") -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.approval_id = approval_id


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
        a2a: A2ARuntimeManager | None = None,
        max_direct_tool_schemas: int = 32,
    ) -> None:
        self.wiki = wiki
        self.memory = memory
        self.skills = skills
        self.events = events
        self.gateway = gateway
        self.a2a = a2a
        self.max_direct_tool_schemas = max_direct_tool_schemas
        self._tools: dict[str, ToolDefinition] = {}
        self._register_builtin_tools()
        if self.gateway is not None:
            self.install_gateway(self.gateway)
        if self.a2a is not None:
            self.install_a2a(self.a2a)

    def list(self) -> list[ToolDefinition]:
        return sorted(self._tools.values(), key=lambda item: item.name)

    def openai_tools(self) -> list[dict[str, Any]]:
        available = [tool for tool in self.list() if tool.available]
        if len(available) <= self.max_direct_tool_schemas:
            return [tool.openai_schema() for tool in available]
        direct_names = {
            "tool_search",
            "tool_describe",
            "tool_call",
            "wiki_orient",
            "wiki_route",
            "wiki_browse",
            "wiki_search",
            "wiki_read",
            "wiki_follow_links",
            "memory_search",
            "skill_search",
            "skill_read",
            "wiki_sufficiency_check",
        }
        return [tool.openai_schema() for tool in available if tool.name in direct_names]

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
                name="wiki_route",
                description="Plan an LLM-Wiki retrieval route: search-first, browse-first, bridge, or insufficient.",
                scope="wiki.read",
                parameters={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                },
                handler=self._wiki_route,
            )
        )
        self.register(
            ToolDefinition(
                name="wiki_browse",
                description="Browse LLM-Wiki pages by path prefix, page type, and index-linked map.",
                scope="wiki.read",
                parameters={
                    "type": "object",
                    "properties": {
                        "path_prefix": {"type": "string"},
                        "page_type": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                },
                handler=self._wiki_browse,
            )
        )
        self.register(
            ToolDefinition(
                name="wiki_search",
                description="Search the Wiki page index with structured ranking and FTS fallback. Use wiki_read before relying on a page as evidence.",
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
                name="wiki_sufficiency_check",
                description="Check whether Wiki-backed claims have page-level evidence from wiki_read.",
                scope="wiki.read",
                parameters={
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "read_pages": {"type": "array", "items": {"type": "string"}},
                        "required_fan_in": {"type": "integer"},
                        "strategy": {"type": "string"},
                    },
                },
                handler=self._wiki_sufficiency_check,
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
                name="wiki_repair",
                description="Repair low-risk LLM-Wiki structure issues and create review proposals for high-risk Error Book items.",
                scope="wiki.write",
                requires_approval=True,
                parameters={
                    "type": "object",
                    "properties": {
                        "apply_safe": {"type": "boolean"},
                        "error_ids": {"type": "array", "items": {"type": "string"}},
                    },
                },
                handler=self._wiki_repair,
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
                name="skill_search",
                description="Search the skill index for procedural guidance. Use before reading a full skill.",
                scope="skill.read",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                },
                handler=self._skill_search,
            )
        )
        self.register(
            ToolDefinition(
                name="skill_read",
                description="Read one skill's indexed files by skill_key after skill_search indicates it is relevant.",
                scope="skill.read",
                parameters={
                    "type": "object",
                    "required": ["skill_key"],
                    "properties": {"skill_key": {"type": "string"}},
                },
                handler=self._skill_read,
            )
        )
        self.register(
            ToolDefinition(
                name="skill_use_trace",
                description="Record that a skill was used and whether it helped, as a skill_trace memory.",
                scope="memory.write",
                requires_approval=False,
                parameters={
                    "type": "object",
                    "required": ["skill_key", "outcome"],
                    "properties": {
                        "skill_key": {"type": "string"},
                        "outcome": {"type": "string"},
                        "notes": {"type": "string"},
                    },
                },
                handler=self._skill_use_trace,
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
        self.register(
            ToolDefinition(
                name="tool_search",
                description="Search available SoulClaw tools by name, description, and scope.",
                scope="system.read",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                },
                handler=self._tool_search,
            )
        )
        self.register(
            ToolDefinition(
                name="tool_describe",
                description="Describe one available tool and return its parameter schema.",
                scope="system.read",
                parameters={
                    "type": "object",
                    "required": ["tool_name"],
                    "properties": {"tool_name": {"type": "string"}},
                },
                handler=self._tool_describe,
            )
        )
        self.register(
            ToolDefinition(
                name="tool_call",
                description="Call a named available tool after discovering it with tool_search/tool_describe.",
                scope="system.write",
                requires_approval=False,
                parameters={
                    "type": "object",
                    "required": ["tool_name"],
                    "properties": {
                        "tool_name": {"type": "string"},
                        "arguments": {"type": "object"},
                    },
                },
                handler=self._tool_call,
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

    def install_a2a(self, a2a: A2ARuntimeManager) -> None:
        self.a2a = a2a
        self.register(
            ToolDefinition(
                name="a2a_delegate",
                description=(
                    "Delegate a coarse-grained task to an enabled A2A specialist agent. "
                    "Use for DeepResearch, document projects, scheduling, or coding agents."
                ),
                scope="external.write",
                requires_approval=False,
                parameters={
                    "type": "object",
                    "required": ["capability", "query"],
                    "properties": {
                        "capability": {"type": "string"},
                        "query": {"type": "string"},
                        "connection_name": {"type": "string"},
                        "context": {"type": "object"},
                        "files": {"type": "array", "items": {"type": "object"}},
                        "options": {"type": "object"},
                    },
                },
                handler=self._a2a_delegate,
            )
        )

    def _wiki_orient(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.wiki.orientation(db, recent_log_lines=int(arguments.get("recent_log_lines") or 40))

    def _wiki_route(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.wiki.route(
            db,
            str(arguments.get("query") or ""),
            limit=int(arguments.get("limit") or 10),
        )

    def _wiki_browse(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.wiki.browse(
            db,
            path_prefix=str(arguments.get("path_prefix") or ""),
            page_type=str(arguments.get("page_type") or ""),
            limit=int(arguments.get("limit") or 100),
        )

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
                    "match_reasons": item.get("match_reasons", []),
                    "matched_fields": item.get("matched_fields", []),
                    "snippet": item.get("snippet", ""),
                    "next_actions": item.get("next_actions", []),
                    "constraints": item.get("constraints", []),
                }
                for item in self.wiki.search(
                    db,
                    query,
                    limit=limit,
                    strategy=str(arguments.get("strategy") or ""),
                    path_prefix=str(arguments.get("path_prefix") or ""),
                    include_constraints=bool(arguments.get("include_constraints", False)),
                )
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

    def _wiki_sufficiency_check(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        read_pages = [str(item).strip() for item in arguments.get("read_pages", []) if str(item).strip()] if isinstance(arguments.get("read_pages"), list) else []
        required = arguments.get("required_fan_in")
        return self.wiki.sufficiency_check(
            db,
            claim=str(arguments.get("claim") or ""),
            read_pages=read_pages,
            required_fan_in=int(required) if required is not None else None,
            strategy=str(arguments.get("strategy") or ""),
        )

    def _wiki_compile(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.wiki.compile(db)

    def _wiki_lint(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        del arguments
        return self.wiki.lint(db)

    def _wiki_repair(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        error_ids = arguments.get("error_ids") if isinstance(arguments.get("error_ids"), list) else []
        return self.wiki.repair(
            db,
            apply_safe=bool(arguments.get("apply_safe", False)),
            error_ids=[str(item) for item in error_ids],
        )

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

    def _skill_search(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "")
        limit = int(arguments.get("limit") or 10)
        return {
            "items": [
                {
                    "skill_key": item["skill"].skill_key,
                    "name": item["skill"].name,
                    "description": item["skill"].description,
                    "status": item["skill"].status,
                    "pinned": item["skill"].pinned,
                    "metadata": item["skill"].metadata_json or {},
                    "source": item["source"],
                    "score": item["score"],
                }
                for item in self.skills.search(db, query, limit=limit)
            ]
        }

    def _skill_read(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        skill_key = str(arguments.get("skill_key") or "")
        skill = self.skills.get(db, skill_key)
        if skill is None:
            raise KeyError(f"skill not found: {skill_key}")
        files = self.skills.files(db, skill_key)
        return {
            "skill_key": skill.skill_key,
            "name": skill.name,
            "description": skill.description,
            "metadata": skill.metadata_json or {},
            "files": [
                {
                    "file_path": item.file_path,
                    "checksum": item.checksum,
                    "content": item.content,
                }
                for item in files
            ],
        }

    def _skill_use_trace(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        skill_key = str(arguments.get("skill_key") or "")
        outcome = str(arguments.get("outcome") or "")
        notes = str(arguments.get("notes") or "")
        content = f"Skill `{skill_key}` outcome: {outcome}"
        if notes.strip():
            content += f" Notes: {notes.strip()}"
        memory = self.memory.create(
            db,
            kind="skill_trace",
            content=content,
            source="skill_use_trace",
            importance=float(arguments.get("importance", 0.45)),
            confidence=float(arguments.get("confidence", 0.65)),
            stability=float(arguments.get("stability", 0.4)),
            metadata={"skill_key": skill_key, "outcome": outcome, "notes": notes},
        )
        return {"memory_id": str(memory.id), "skill_key": skill_key, "outcome": outcome}

    def _skills_scan(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.skills.scan(db)

    def _skill_test(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.skills.test(db, str(arguments["skill_key"]))

    def _tool_search(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        del db
        query = str(arguments.get("query") or "").strip().lower()
        limit = max(1, min(int(arguments.get("limit") or 20), 100))
        items = []
        for tool in self.list():
            haystack = f"{tool.name} {tool.description} {tool.scope}".lower()
            if query and query not in haystack:
                continue
            items.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "scope": tool.scope,
                    "requires_approval": tool.requires_approval,
                    "available": tool.available,
                    "score": 1.0 if query and query in tool.name.lower() else 0.7,
                }
            )
        return {"items": items[:limit], "total": len(items)}

    def _tool_describe(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        del db
        tool_name = str(arguments.get("tool_name") or "")
        tool = self.get(tool_name)
        if tool is None:
            raise KeyError(f"tool not found: {tool_name}")
        return {
            "name": tool.name,
            "description": tool.description,
            "scope": tool.scope,
            "requires_approval": tool.requires_approval,
            "available": tool.available,
            "parameters": tool.parameters,
        }

    def _tool_call(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        tool_name = str(arguments.get("tool_name") or "")
        if tool_name in {"tool_call", ""}:
            raise ValueError("tool_call requires a non-recursive target tool_name")
        tool = self.get(tool_name)
        if tool is None or tool.handler is None:
            raise KeyError(f"tool not found: {tool_name}")
        if not tool.available:
            raise RuntimeError(f"tool unavailable: {tool_name}")
        call_arguments = arguments.get("arguments") if isinstance(arguments.get("arguments"), dict) else {}
        if (tool.requires_approval or tool.scope in ToolSafetyFloor.mutating_scopes) and not bool(arguments.get("_approved")):
            raise PermissionError(f"tool requires approval: {tool_name}")
        if bool(arguments.get("_approved")):
            call_arguments = {**call_arguments, "_approved": True}
        return {"tool_name": tool_name, "result": tool.handler(db, call_arguments)}

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

    def _a2a_delegate(self, db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.a2a is None:
            raise RuntimeError("A2A runtime is not configured")
        from backend.runtime.a2a import A2ADelegateRequest

        capability = str(arguments.get("capability") or "")
        options = arguments.get("options") if isinstance(arguments.get("options"), dict) else {}
        if self.a2a.requires_approval(capability, options) and not bool(arguments.get("_approved")):
            raise PermissionError("A2A delegation requires approval for high-risk capability")
        return self.a2a.delegate(
            db,
            A2ADelegateRequest(
                capability=capability,
                query=str(arguments.get("query") or ""),
                context=arguments.get("context") if isinstance(arguments.get("context"), dict) else {},
                files=arguments.get("files") if isinstance(arguments.get("files"), list) else [],
                options=options,
                connection_name=str(arguments.get("connection_name") or ""),
            ),
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
                approval = self._create_tool_approval(db, tool_name=tool_name, arguments=arguments, turn_id=turn_id)
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
            raise ToolApprovalRequired(
                f"tool requires approval: {tool_name}",
                tool_name=tool_name,
                approval_id=str(approval.id) if approval is not None else "",
            )
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
        handler_arguments = dict(arguments)
        if approved:
            handler_arguments["_approved"] = True
        try:
            result = definition.handler(db, handler_arguments)
        except PermissionError as exc:
            approval = None
            if self.platform is not None:
                approval = self._create_tool_approval(
                    db,
                    tool_name=tool_name,
                    arguments=arguments,
                    turn_id=turn_id,
                    dynamic_approval=True,
                )
            run.status = "approval_required"
            run.result = {"error": str(exc), "approval_id": str(approval.id) if approval is not None else None}
            run.finished_at = datetime.now(UTC)
            if self.events:
                self.events.emit(
                    "tool.approval_required",
                    {
                        "tool_run_id": str(run.id),
                        "tool_name": tool_name,
                        "approval_id": str(approval.id) if approval is not None else None,
                        "error": str(exc),
                    },
                    severity="warning",
                    turn_id=turn_id,
                )
            raise ToolApprovalRequired(
                str(exc),
                tool_name=tool_name,
                approval_id=str(approval.id) if approval is not None else "",
            ) from exc
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

    def _create_tool_approval(
        self,
        db: Session,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        turn_id: str,
        dynamic_approval: bool = False,
    ):
        if self.platform is None:
            return None
        payload = {"tool_name": tool_name, "arguments": arguments, "turn_id": turn_id}
        if dynamic_approval:
            payload["dynamic_approval"] = True
        try:
            return self.platform.create_approval(
                db,
                subject_type="tool_run",
                subject_id=tool_name,
                payload=payload,
                original_tool_call={"tool_name": tool_name, "arguments": arguments},
                turn_checkpoint={"turn_id": turn_id, "tool_name": tool_name},
                allowed_decisions=["approve", "edit", "reject", "respond"],
                resume_state={"mode": "rerun_tool"},
            )
        except TypeError:
            return self.platform.create_approval(
                db,
                subject_type="tool_run",
                subject_id=tool_name,
                payload=payload,
            )
