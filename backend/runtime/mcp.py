"""MCP discovery and transient tool execution for the ZLAgent runtime."""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.domain.tools import ToolDefinition
from backend.infra.db import session_scope
from backend.infra.events import RuntimeEventBus
from backend.infra.models import MCPServer


@dataclass(frozen=True)
class MCPToolDescriptor:
    server_name: str
    tool_name: str
    description: str
    input_schema: dict[str, Any]

    @property
    def registry_name(self) -> str:
        return f"mcp__{safe_tool_token(self.server_name)}__{safe_tool_token(self.tool_name)}"

    def to_cache(self) -> dict[str, Any]:
        return {
            "server_name": self.server_name,
            "tool_name": self.tool_name,
            "registry_name": self.registry_name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    @classmethod
    def from_cache(cls, item: dict[str, Any]) -> MCPToolDescriptor:
        return cls(
            server_name=str(item.get("server_name") or ""),
            tool_name=str(item.get("tool_name") or ""),
            description=str(item.get("description") or ""),
            input_schema=item.get("input_schema") if isinstance(item.get("input_schema"), dict) else {},
        )


def safe_tool_token(value: str) -> str:
    token = re.sub(r"[^a-zA-Z0-9_]", "_", value.strip())
    token = re.sub(r"_+", "_", token).strip("_")
    if not token:
        token = "unnamed"
    if token[0].isdigit():
        token = f"_{token}"
    return token[:64]


class MCPRuntimeManager:
    """Discovers MCP tools from configured servers and exposes tool wrappers.

    Connections are intentionally transient in this cut. Discovery stores a
    cache in Postgres; execution opens a fresh MCP client session, calls one
    tool, then closes it. This avoids long-lived child processes during local
    development and gives the platform a durable audit surface first.
    """

    def __init__(
        self,
        *,
        events: RuntimeEventBus,
        discovery_timeout_seconds: float = 12.0,
        call_timeout_seconds: float = 60.0,
    ) -> None:
        self.events = events
        self.discovery_timeout_seconds = discovery_timeout_seconds
        self.call_timeout_seconds = call_timeout_seconds

    async def refresh_all(self) -> dict[str, Any]:
        with session_scope() as db:
            names = list(db.scalars(select(MCPServer.name).where(MCPServer.enabled.is_(True))).all())
        results = []
        for name in names:
            results.append(await self.refresh_server(name))
        return {
            "servers": len(results),
            "tools": sum(int(item.get("tool_count") or 0) for item in results),
            "items": results,
        }

    async def refresh_server(self, server_name: str) -> dict[str, Any]:
        with session_scope() as db:
            server = db.scalar(select(MCPServer).where(MCPServer.name == server_name))
            if server is None:
                raise KeyError(f"MCP server not found: {server_name}")
            config = _server_config(server)
            enabled = bool(server.enabled)
        if not enabled:
            return self._persist_status(server_name, status="disabled", tools=[], error="")

        try:
            descriptors = await asyncio.wait_for(
                self._discover(config),
                timeout=self.discovery_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            logger.warning("[mcp] discovery failed for {}: {}", server_name, error)
            self.events.emit("mcp.discovery.failed", {"server": server_name, "error": error}, severity="warning")
            return self._persist_status(server_name, status="failed", tools=[], error=error)

        result = self._persist_status(server_name, status="connected", tools=descriptors, error="")
        self.events.emit(
            "mcp.discovery.succeeded",
            {"server": server_name, "tool_count": result["tool_count"]},
        )
        return result

    def cached_tool_definitions(self) -> list[ToolDefinition]:
        with session_scope() as db:
            servers = list(db.scalars(select(MCPServer).where(MCPServer.enabled.is_(True))).all())
            descriptors: list[MCPToolDescriptor] = []
            connected_by_name = {server.name: server.status == "connected" for server in servers}
            for server in servers:
                for item in server.tools_cache or []:
                    if isinstance(item, dict):
                        descriptors.append(MCPToolDescriptor.from_cache(item))
        return [self._tool_definition(descriptor, available=connected_by_name.get(descriptor.server_name, False)) for descriptor in descriptors]

    def install_into_registry(self, registry) -> dict[str, int]:
        removed = registry.unregister_prefix("mcp__")
        definitions = self.cached_tool_definitions()
        registry.register_many(definitions)
        return {"removed": removed, "registered": len(definitions)}

    def call_tool_sync(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.call_tool(server_name, tool_name, arguments))
        raise RuntimeError("MCP tools must be executed from a worker thread in the synchronous tool executor")

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        with session_scope() as db:
            server = db.scalar(select(MCPServer).where(MCPServer.name == server_name))
            if server is None:
                raise KeyError(f"MCP server not found: {server_name}")
            if not server.enabled:
                raise RuntimeError(f"MCP server disabled: {server_name}")
            config = _server_config(server)
        self.events.emit("mcp.tool.started", {"server": server_name, "tool": tool_name})
        try:
            async with self._client_session(config) as session:
                result = await session.call_tool(
                    tool_name,
                    arguments or {},
                    read_timeout_seconds=timedelta(seconds=self.call_timeout_seconds),
                )
        except Exception as exc:  # noqa: BLE001
            self.events.emit(
                "mcp.tool.failed",
                {"server": server_name, "tool": tool_name, "error": str(exc)},
                severity="warning",
            )
            raise
        payload = serialize_mcp_result(result)
        self.events.emit("mcp.tool.succeeded", {"server": server_name, "tool": tool_name})
        return payload

    async def _discover(self, config: dict[str, Any]) -> list[MCPToolDescriptor]:
        async with self._client_session(config) as session:
            result = await session.list_tools()
        tools = getattr(result, "tools", []) or []
        descriptors: list[MCPToolDescriptor] = []
        server_name = str(config["name"])
        for tool in tools:
            tool_name = str(getattr(tool, "name", ""))
            if not tool_name:
                continue
            input_schema = getattr(tool, "inputSchema", None)
            descriptors.append(
                MCPToolDescriptor(
                    server_name=server_name,
                    tool_name=tool_name,
                    description=str(getattr(tool, "description", "") or ""),
                    input_schema=input_schema if isinstance(input_schema, dict) else {},
                )
            )
        return descriptors

    @asynccontextmanager
    async def _client_session(self, config: dict[str, Any]) -> AsyncIterator[ClientSession]:
        async with self._transport_streams(config) as streams:
            read_stream, write_stream = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session

    @asynccontextmanager
    async def _transport_streams(self, config: dict[str, Any]) -> AsyncIterator[tuple[Any, Any]]:
        transport = str(config["transport"])
        if transport == "stdio":
            params = StdioServerParameters(
                command=str(config["command"]),
                args=list(config.get("args") or []),
                env=config.get("env") if isinstance(config.get("env"), dict) else None,
                cwd=config.get("cwd") or None,
            )
            async with stdio_client(params) as (read_stream, write_stream):
                yield read_stream, write_stream
            return
        if transport == "sse":
            async with sse_client(
                str(config["url"]),
                headers=config.get("headers") if isinstance(config.get("headers"), dict) else None,
                timeout=float(config.get("timeout") or 5),
                sse_read_timeout=float(config.get("sse_read_timeout") or 300),
            ) as (read_stream, write_stream):
                yield read_stream, write_stream
            return
        if transport in {"streamable_http", "http"}:
            async with streamablehttp_client(
                str(config["url"]),
                headers=config.get("headers") if isinstance(config.get("headers"), dict) else None,
                timeout=float(config.get("timeout") or 30),
                sse_read_timeout=float(config.get("sse_read_timeout") or 300),
            ) as streams:
                read_stream, write_stream, _session_id = streams
                yield read_stream, write_stream
            return
        raise ValueError(f"unsupported MCP transport: {transport}")

    def _persist_status(
        self,
        server_name: str,
        *,
        status: str,
        tools: list[MCPToolDescriptor],
        error: str,
    ) -> dict[str, Any]:
        cache = [tool.to_cache() for tool in tools]
        with session_scope() as db:
            server = db.scalar(select(MCPServer).where(MCPServer.name == server_name))
            if server is None:
                raise KeyError(f"MCP server not found: {server_name}")
            server.status = status
            server.last_error = error
            server.tool_count = len(cache)
            server.tools_cache = cache
            server.last_connected_at = datetime.now(UTC) if status == "connected" else server.last_connected_at
            db.flush()
            return {
                "name": server.name,
                "status": server.status,
                "tool_count": server.tool_count,
                "last_error": server.last_error,
            }

    def _tool_definition(self, descriptor: MCPToolDescriptor, *, available: bool) -> ToolDefinition:
        def handler(db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
            del db
            return self.call_tool_sync(descriptor.server_name, descriptor.tool_name, arguments)

        return ToolDefinition(
            name=descriptor.registry_name,
            description=descriptor.description or f"MCP tool {descriptor.tool_name} from {descriptor.server_name}.",
            scope="external.write",
            requires_approval=True,
            available=available,
            parameters=normalize_json_schema(descriptor.input_schema),
            handler=handler,
        )


def _server_config(server: MCPServer) -> dict[str, Any]:
    config = dict(server.config or {})
    command = str(config.pop("command", server.command) or server.command)
    args = config.pop("args", None)
    if server.transport == "stdio" and not args:
        args = config.pop("argv", None)
    if server.transport == "stdio" and not args:
        parts = shlex.split(command)
        if parts:
            command = parts[0]
            args = parts[1:]
    return {
        **config,
        "name": server.name,
        "transport": "streamable_http" if server.transport == "http" else server.transport,
        "command": command,
        "args": args or [],
        "url": str(config.get("url") or server.url),
    }


def normalize_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    if not schema:
        return {"type": "object", "properties": {}, "additionalProperties": True}
    normalized = dict(schema)
    normalized.setdefault("type", "object")
    normalized.setdefault("properties", {})
    return normalized


def serialize_mcp_result(result: Any) -> dict[str, Any]:
    content = getattr(result, "content", []) or []
    return {
        "content": [serialize_content_item(item) for item in content],
        "is_error": bool(getattr(result, "isError", False)),
        "structured_content": getattr(result, "structuredContent", None),
    }


def serialize_content_item(item: Any) -> dict[str, Any]:
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json", by_alias=True)
    try:
        return json.loads(json.dumps(item))
    except TypeError:
        return {"type": "text", "text": str(item)}
