"""End-to-end tests for the MCP Python SDK v2 low-level ``Server`` adapter.

v2 replaced the public ``request_handlers`` dict (keyed by request class) with
constructor-injected handlers stored in ``_request_handlers`` (keyed by method
string) behind ``add_request_handler``/``get_request_handler``. Handlers take
``(ctx, params)`` and raise through to JSON-RPC errors instead of auto-
converting to ``is_error`` results.
"""

import pytest

import mcp.types as mcp_types
from mcp.server.lowlevel import Server

from posthog.mcp import instrument
from posthog.mcp.types import MCPAnalyticsOptions
from posthog.test.mcp._helpers import (
    FakeClient,
    events_named as _events,
    flush_background as _flush,
)
from posthog.test.mcp._helpers_v2 import fake_ctx


def make_server():
    async def on_call_tool(ctx, params):
        if params.name == "boom":
            raise ValueError("explode")
        if params.name == "soft-fail":
            return mcp_types.CallToolResult(
                content=[mcp_types.TextContent(type="text", text="failed politely")],
                is_error=True,
            )
        args = params.arguments or {}
        return mcp_types.CallToolResult(
            content=[
                mcp_types.TextContent(
                    type="text", text=str(args.get("a", 0) + args.get("b", 0))
                )
            ]
        )

    async def on_list_tools(ctx, params):
        return mcp_types.ListToolsResult(
            tools=[
                mcp_types.Tool(
                    name="add",
                    description="Add two numbers",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "a": {"type": "integer"},
                            "b": {"type": "integer"},
                        },
                        "required": ["a", "b"],
                    },
                )
            ]
        )

    return Server(
        "test-low-v2",
        version="1.2.3",
        on_call_tool=on_call_tool,
        on_list_tools=on_list_tools,
    )


async def _call_tool(server, name, arguments, ctx=None):
    entry = server.get_request_handler("tools/call")
    params = mcp_types.CallToolRequestParams(name=name, arguments=arguments)
    return await entry.handler(ctx or fake_ctx(), params)


async def _list_tools(server, ctx=None):
    entry = server.get_request_handler("tools/list")
    return await entry.handler(ctx or fake_ctx(method="tools/list"), None)


# --- tools/list --------------------------------------------------------------


async def test_list_tools_injects_optional_context_and_captures():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    result = await _list_tools(server)
    await _flush()

    add_tool = next(t for t in result.tools if t.name == "add")
    # optional on the raw low-level path: this schema is also the call's
    # validation schema, so `context` must not become required
    assert "context" in add_tool.input_schema["properties"]
    assert "context" not in add_tool.input_schema.get("required", [])

    listed = _events(client, "$mcp_tools_list")
    assert listed
    assert listed[0]["properties"]["$mcp_listed_tool_names"] == ["add"]
    assert listed[0]["properties"]["$mcp_server_name"] == "test-low-v2"


# --- tools/call --------------------------------------------------------------


async def test_tool_call_captured_with_client_identity():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    # Prime the listing metadata used by the shared call lifecycle.
    await _list_tools(server)
    result = await _call_tool(
        server, "add", {"a": 2, "b": 3, "context": "adding for a report"}
    )
    await _flush()

    assert result.content[0].text == "5"

    calls = _events(client, "$mcp_tool_call")
    assert len(calls) == 1
    props = calls[0]["properties"]
    assert props["$mcp_tool_name"] == "add"
    assert props["$mcp_tool_description"] == "Add two numbers"
    assert props["$mcp_intent"] == "adding for a report"
    assert props["$mcp_is_error"] is False
    assert props["$mcp_client_name"] == "test-client"
    assert props["$mcp_protocol_version"] == "2026-07-28"


async def test_context_left_in_arguments_for_raw_handlers():
    """On the raw low-level path injected keys are NOT stripped — the schema
    advertises them as optional and a ``(name, arguments)`` handler ignores
    extra keys."""
    seen = {}

    async def on_call_tool(ctx, params):
        seen["arguments"] = dict(params.arguments or {})
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="ok")]
        )

    server = Server("raw", on_call_tool=on_call_tool)
    client = FakeClient()
    instrument(server, client)

    await _call_tool(
        server, "anything", {"x": 1, "context": "raw handlers see everything"}
    )
    await _flush()

    assert seen["arguments"] == {"x": 1, "context": "raw handlers see everything"}


async def test_raised_error_is_captured_and_reraised():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    with pytest.raises(ValueError):
        await _call_tool(server, "boom", {"context": "attempting the risky operation"})
    await _flush()

    calls = _events(client, "$mcp_tool_call")
    assert calls and calls[0]["properties"]["$mcp_is_error"] is True
    exceptions = _events(client, "$exception")
    assert exceptions
    assert exceptions[0]["properties"]["$exception_list"][-1]["value"] == "explode"


async def test_is_error_result_is_captured():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    result = await _call_tool(
        server, "soft-fail", {"context": "expecting a polite failure"}
    )
    await _flush()

    assert result.is_error is True
    calls = _events(client, "$mcp_tool_call")
    assert calls and calls[0]["properties"]["$mcp_is_error"] is True


async def test_late_registration_is_wrapped():
    """Handlers registered via ``add_request_handler`` *after* ``instrument()``
    must still be wrapped (the JS #4449 lesson: adapters that hand over a bare
    server and register handlers afterwards)."""
    server = Server("late-reg")  # no handlers yet
    client = FakeClient()
    instrument(server, client)

    async def late_call_tool(ctx, params):
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="late ok")]
        )

    server.add_request_handler(
        "tools/call", mcp_types.CallToolRequestParams, late_call_tool
    )

    result = await _call_tool(server, "anything", {"context": "late registration"})
    await _flush()

    assert result.content[0].text == "late ok"
    calls = _events(client, "$mcp_tool_call")
    assert len(calls) == 1
    assert calls[0]["properties"]["$mcp_tool_name"] == "anything"


async def test_initialize_and_session_reuse_across_calls():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    await _call_tool(server, "add", {"a": 1, "b": 1, "context": "first"})
    await _call_tool(server, "add", {"a": 2, "b": 2, "context": "second"})
    await _flush()

    assert len(_events(client, "$mcp_initialize")) == 1
    calls = _events(client, "$mcp_tool_call")
    session_ids = {c["properties"]["$session_id"] for c in calls}
    assert len(session_ids) == 1


async def test_instrument_is_idempotent():
    server = make_server()
    client = FakeClient()
    instrument(server, client)
    wrapped = server.get_request_handler("tools/call").handler
    instrument(server, client)
    assert server.get_request_handler("tools/call").handler is wrapped


async def test_report_missing_appends_virtual_tool():
    server = make_server()
    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(report_missing=True))

    result = await _list_tools(server)
    names = [t.name for t in result.tools]
    assert "get_more_tools" in names

    call_result = await _call_tool(
        server, "get_more_tools", {"context": "need an email tool"}
    )
    await _flush()

    assert call_result.is_error is False
    missing = _events(client, "$mcp_missing_capability")
    assert missing and missing[0]["properties"]["$mcp_intent"] == "need an email tool"


async def test_callbacks_can_read_headers_through_the_helper():
    """The same `identify` body must work on both SDK majors: `extra["ctx"]` is
    the SDK's own context and `get_request_headers` normalises the read."""
    from posthog.mcp import get_request_headers

    server = make_server()
    client = FakeClient()
    seen = {}

    def identify(request, extra):
        seen["headers"] = get_request_headers(extra)
        return None

    instrument(server, client, MCPAnalyticsOptions(identify=identify))

    ctx = fake_ctx(headers={"Authorization": "Bearer t0ken", "User-Agent": "probe/1"})
    await _call_tool(server, "add", {"a": 1, "b": 1, "context": "header read"}, ctx=ctx)
    await _flush()

    assert seen["headers"] == {"authorization": "Bearer t0ken", "user-agent": "probe/1"}
