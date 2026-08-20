"""End-to-end tests for the MCP Python SDK v2 high-level server adapter
(``mcp.server.mcpserver.MCPServer``, the renamed FastMCP).

Mirrors ``test_fastmcp.py``; drives the wrapped seams directly — the low-level
``_request_handlers`` entries for tools/list and tools/call — with a fake
``ServerRequestContext``, exactly as the v2 runner would invoke them.
"""

import mcp.types as mcp_types
from mcp.server.mcpserver import MCPServer

from posthog.mcp import instrument
from posthog.mcp.types import MCPAnalyticsOptions, UserIdentity
from posthog.test.mcp._helpers import (
    FakeClient,
    events_named as _events,
    flush_background as _flush,
)
from posthog.test.mcp._helpers_v2 import fake_ctx


def make_server():
    server = MCPServer("test-server-v2")

    @server.tool()
    def add(a: int, b: int) -> int:
        return a + b

    @server.tool()
    def boom() -> str:
        raise ValueError("explode")

    return server


async def _list_tools(server, ctx=None):
    entry = server._lowlevel_server.get_request_handler("tools/list")
    return await entry.handler(ctx or fake_ctx(method="tools/list"), None)


async def _call_tool(server, name, arguments, ctx=None):
    entry = server._lowlevel_server.get_request_handler("tools/call")
    params = mcp_types.CallToolRequestParams(name=name, arguments=arguments)
    return await entry.handler(ctx or fake_ctx(), params)


# --- tools/list --------------------------------------------------------------


async def test_list_tools_injects_context_and_captures():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    result = await _list_tools(server)
    await _flush()

    add_tool = next(t for t in result.tools if t.name == "add")
    assert "context" in add_tool.input_schema["properties"]
    assert "context" in add_tool.input_schema["required"]

    listed = _events(client, "$mcp_tools_list")
    assert listed
    assert set(listed[0]["properties"]["$mcp_listed_tool_names"]) == {"add", "boom"}
    assert listed[0]["properties"]["$mcp_client_name"] == "test-client"
    assert listed[0]["properties"]["$mcp_protocol_version"] == "2026-07-28"


async def test_context_injection_can_be_disabled():
    server = make_server()
    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(context=False))

    result = await _list_tools(server)
    add_tool = next(t for t in result.tools if t.name == "add")
    assert "context" not in add_tool.input_schema.get("properties", {})


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

    result = await _call_tool(
        server,
        "add",
        {"a": 2, "b": 3, "context": "summing two numbers for the user's report"},
    )
    await _flush()

    # the tool executed cleanly and the injected `context` never reached it
    assert result.is_error is False
    assert received["args"] == {"a": 2, "b": 3}

    calls = _events(client, "$mcp_tool_call")
    assert len(calls) == 1
    props = calls[0]["properties"]
    assert props["$mcp_tool_name"] == "add"
    assert props["$mcp_intent"] == "summing two numbers for the user's report"
    assert props["$mcp_intent_source"] == "context_parameter"
    assert props["$mcp_is_error"] is False
    assert props["$mcp_client_name"] == "test-client"
    assert props["$mcp_client_version"] == "9.9.9"
    assert props["$mcp_protocol_version"] == "2026-07-28"
    assert "$mcp_duration_ms" in props
    # context is stripped from captured parameters too
    assert "context" not in props["$mcp_parameters"]["request"]["params"]["arguments"]


async def test_tool_owning_context_keeps_it():
    server = make_server()

    received = {}

    @server.tool()
    def search(query: str, context: str) -> str:
        received["context"] = context
        return f"{query}!"

    client = FakeClient()
    instrument(server, client)

    listed = await _list_tools(server)
    search_tool = next(t for t in listed.tools if t.name == "search")
    # the tool's own `context` parameter is not clobbered by injection
    assert search_tool.input_schema["properties"]["context"]["type"] == "string"

    await _call_tool(server, "search", {"query": "q", "context": "the real argument"})
    await _flush()

    assert received["context"] == "the real argument"


async def test_tool_call_error_is_captured_and_converted():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    # MCPServer converts the raise into CallToolResult(is_error=True) *outside*
    # the wrapped ToolManager seam, so the client still gets the error result...
    result = await _call_tool(
        server, "boom", {"context": "attempting the risky operation"}
    )
    await _flush()
    assert result.is_error is True

    # ...and the wrapper saw the raise: error event + $exception sibling.
    calls = _events(client, "$mcp_tool_call")
    assert calls and calls[0]["properties"]["$mcp_is_error"] is True
    exceptions = _events(client, "$exception")
    assert exceptions
    exception_list = exceptions[0]["properties"]["$exception_list"]
    assert exception_list[-1]["value"] == "explode"


async def test_initialize_emitted_once_per_session():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    await _call_tool(
        server, "add", {"a": 1, "b": 1, "context": "first call to warm up"}
    )
    await _call_tool(
        server, "add", {"a": 2, "b": 2, "context": "second call for the total"}
    )
    await _flush()

    assert len(_events(client, "$mcp_initialize")) == 1
    assert len(_events(client, "$mcp_tool_call")) == 2


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

    await _call_tool(
        server, "add", {"a": 1, "b": 2, "context": "checking identity flows through"}
    )
    await _flush()

    calls = _events(client, "$mcp_tool_call")
    assert calls[0]["distinct_id"] == "user_42"
    assert calls[0]["properties"]["$groups"] == {"organization": "org_7"}
    assert _events(client, "$identify")


async def test_report_missing_advertises_and_captures():
    server = make_server()
    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(report_missing=True))

    listed = await _list_tools(server)
    names = [t.name for t in listed.tools]
    assert "get_more_tools" in names

    result = await _call_tool(
        server, "get_more_tools", {"context": "need a tool to send emails"}
    )
    await _flush()

    assert result.is_error is False
    missing = _events(client, "$mcp_missing_capability")
    assert missing
    assert missing[0]["properties"]["$mcp_intent"] == "need a tool to send emails"


async def test_instrument_is_idempotent():
    server = make_server()
    client = FakeClient()
    instrument(server, client)
    wrapped_call = server._tool_manager.call_tool
    instrument(server, client)
    assert server._tool_manager.call_tool is wrapped_call  # not double-wrapped


async def test_tool_error_reraise_preserved_for_mcp_layer():
    """The wrapper re-raises: MCPServer's own conversion still yields the same
    error text an uninstrumented server produces (analytics never mutates the
    tool path)."""
    bare = make_server()
    bare_entry = bare._lowlevel_server.get_request_handler("tools/call")
    bare_result = await bare_entry.handler(
        fake_ctx(), mcp_types.CallToolRequestParams(name="boom", arguments={})
    )

    instrumented = make_server()
    instrument(instrumented, FakeClient())
    result = await _call_tool(instrumented, "boom", {"context": "same failure"})
    await _flush()

    assert result.is_error is True
    assert result.content[0].text == bare_result.content[0].text


async def test_late_registered_tool_is_covered():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    @server.tool()
    def late(x: int) -> int:
        return x * 2

    result = await _call_tool(server, "late", {"x": 21, "context": "doubling a number"})
    await _flush()

    assert result.is_error is False
    calls = _events(client, "$mcp_tool_call")
    assert calls and calls[0]["properties"]["$mcp_tool_name"] == "late"


async def test_anonymous_events_do_not_create_person_profiles():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    await _call_tool(server, "add", {"a": 1, "b": 1, "context": "anonymous call"})
    await _flush()

    calls = _events(client, "$mcp_tool_call")
    assert calls[0]["properties"]["$process_person_profile"] is False
