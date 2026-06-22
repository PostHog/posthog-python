"""End-to-end tests for the FastMCP adapter (Milestone 2)."""

import asyncio

import pytest

import mcp.types as mcp_types
from mcp.server.fastmcp import FastMCP

from posthog.mcp import instrument
from posthog.mcp.types import MCPAnalyticsOptions, UserIdentity


class FakeClient:
    """Records capture() calls instead of sending them."""

    def __init__(self):
        self.events = []

    def capture(
        self,
        event,
        distinct_id=None,
        properties=None,
        timestamp=None,
        uuid=None,
        **kwargs,
    ):
        self.events.append(
            {"event": event, "distinct_id": distinct_id, "properties": properties or {}}
        )
        return None


def make_server():
    server = FastMCP("test-server")

    @server.tool()
    def add(a: int, b: int) -> int:
        return a + b

    @server.tool()
    def boom() -> str:
        raise ValueError("explode")

    return server


async def _flush():
    """Let fire-and-forget capture tasks run to completion."""
    import posthog.mcp.instrumentation as instr

    for _ in range(10):
        await asyncio.sleep(0)
        pending = [t for t in list(instr._BACKGROUND_TASKS) if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.sleep(0)


def _events(client, name):
    return [e for e in client.events if e["event"] == name]


async def _list_tools(server):
    handler = server._mcp_server.request_handlers[mcp_types.ListToolsRequest]
    return await handler(mcp_types.ListToolsRequest(method="tools/list"))


# --- tools/list --------------------------------------------------------------


async def test_list_tools_injects_context_and_captures():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    result = await _list_tools(server)
    await _flush()

    tools = result.root.tools
    add_tool = next(t for t in tools if t.name == "add")
    assert "context" in add_tool.inputSchema["properties"]
    assert "context" in add_tool.inputSchema["required"]

    listed = _events(client, "$mcp_tools_list")
    assert listed
    assert set(listed[0]["properties"]["$mcp_listed_tool_names"]) == {"add", "boom"}


async def test_context_injection_can_be_disabled():
    server = make_server()
    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(context=False))

    result = await _list_tools(server)
    add_tool = next(t for t in result.root.tools if t.name == "add")
    assert "context" not in add_tool.inputSchema.get("properties", {})


# --- tools/call --------------------------------------------------------------


async def test_tool_call_captures_intent_and_strips_context():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    received = {}
    original_add = server._tool_manager.get_tool("add").fn

    def spy_add(a: int, b: int) -> int:
        received["args"] = {"a": a, "b": b}
        return original_add(a, b)

    server._tool_manager.get_tool("add").fn = spy_add

    await server._tool_manager.call_tool(
        "add", {"a": 2, "b": 3, "context": "summing two numbers for the user's report"}
    )
    await _flush()

    # the injected `context` never reached the tool implementation
    assert received["args"] == {"a": 2, "b": 3}

    calls = _events(client, "$mcp_tool_call")
    assert len(calls) == 1
    props = calls[0]["properties"]
    assert props["$mcp_tool_name"] == "add"
    assert props["$mcp_intent"] == "summing two numbers for the user's report"
    assert props["$mcp_intent_source"] == "context_parameter"
    assert props["$mcp_is_error"] is False
    assert "$mcp_duration_ms" in props
    # context is stripped from captured parameters too
    assert "context" not in props["$mcp_parameters"]["request"]["params"]["arguments"]


async def test_initialize_emitted_once_per_session():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    await server._tool_manager.call_tool(
        "add", {"a": 1, "b": 1, "context": "first call to warm up"}
    )
    await server._tool_manager.call_tool(
        "add", {"a": 2, "b": 2, "context": "second call for the total"}
    )
    await _flush()

    assert len(_events(client, "$mcp_initialize")) == 1
    assert len(_events(client, "$mcp_tool_call")) == 2


async def test_tool_call_error_is_captured_and_reraised():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    with pytest.raises(Exception):
        await server._tool_manager.call_tool(
            "boom", {"context": "attempting the risky operation"}
        )
    await _flush()

    calls = _events(client, "$mcp_tool_call")
    assert calls and calls[0]["properties"]["$mcp_is_error"] is True
    exceptions = _events(client, "$exception")
    assert exceptions
    assert exceptions[0]["properties"]["$exception_list"][0]["value"] == "explode"


async def test_identify_sets_distinct_id_and_groups():
    server = make_server()
    client = FakeClient()
    instrument(
        server,
        client,
        MCPAnalyticsOptions(
            identify=lambda request, extra: UserIdentity(
                distinct_id="user_42",
                properties={"plan": "pro"},
                groups={"organization": "org_7"},
            )
        ),
    )

    await server._tool_manager.call_tool(
        "add", {"a": 1, "b": 2, "context": "checking identity flows through"}
    )
    await _flush()

    calls = _events(client, "$mcp_tool_call")
    assert calls[0]["distinct_id"] == "user_42"
    assert calls[0]["properties"]["$groups"] == {"organization": "org_7"}
    assert "$process_person_profile" not in calls[0]["properties"]
    # an $identify event was emitted
    assert _events(client, "$identify")


async def test_instrument_is_idempotent():
    server = make_server()
    client = FakeClient()
    instrument(server, client)
    wrapped_call = server._tool_manager.call_tool
    instrument(server, client)
    assert server._tool_manager.call_tool is wrapped_call  # not double-wrapped


async def test_unsupported_server_returns_noop_handle():
    handle = instrument(object(), FakeClient())
    # graceful no-op: capture does nothing and does not raise
    await handle.capture("anything")
