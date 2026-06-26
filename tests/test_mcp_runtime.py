from __future__ import annotations

import pytest

from backend.runtime.mcp import (
    MCPRuntimeManager,
    MCPToolDescriptor,
    _server_config,
    safe_tool_token,
)


class DummyEvents:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event_type, payload=None, **kwargs) -> None:
        self.events.append((event_type, payload or {}, kwargs))


class DummyServer:
    name = "local fs"
    transport = "stdio"
    command = "python -m sample_server"
    url = ""
    config = {}


def test_mcp_safe_tool_token_makes_openai_compatible_names() -> None:
    assert safe_tool_token("filesystem/read-file") == "filesystem_read_file"
    assert safe_tool_token("123") == "_123"
    assert safe_tool_token("  ") == "unnamed"


def test_mcp_descriptor_builds_registry_name_and_cache() -> None:
    descriptor = MCPToolDescriptor(
        server_name="local fs",
        tool_name="read-file",
        description="Read a file",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )

    cached = descriptor.to_cache()

    assert cached["registry_name"] == "mcp__local_fs__read_file"
    assert MCPToolDescriptor.from_cache(cached).input_schema["properties"]["path"]["type"] == "string"


def test_mcp_server_config_splits_stdio_command_for_compatibility() -> None:
    config = _server_config(DummyServer())

    assert config["command"] == "python"
    assert config["args"] == ["-m", "sample_server"]


def test_mcp_tool_definition_is_gated_external_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = MCPRuntimeManager(events=DummyEvents())
    descriptor = MCPToolDescriptor(
        server_name="server",
        tool_name="echo",
        description="Echo",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    )
    monkeypatch.setattr(manager, "call_tool_sync", lambda server, tool, args: {"server": server, "tool": tool, "args": args})

    definition = manager._tool_definition(descriptor, available=True)
    result = definition.handler(None, {"text": "hello"})

    assert definition.name == "mcp__server__echo"
    assert definition.scope == "external.write"
    assert definition.requires_approval is True
    assert definition.parameters["properties"]["text"]["type"] == "string"
    assert result["args"] == {"text": "hello"}


def test_mcp_safe_permission_override_registers_read_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = MCPRuntimeManager(events=DummyEvents())
    descriptor = MCPToolDescriptor(
        server_name="server",
        tool_name="search",
        description="Search",
        input_schema={},
        permission="safe",
    )
    monkeypatch.setattr(manager, "call_tool_sync", lambda server, tool, args: {"ok": True})

    definition = manager._tool_definition(descriptor, available=True)

    assert definition.scope == "external.read"
    assert definition.requires_approval is False
