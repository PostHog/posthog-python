"""End-to-end tests for the low-level mcp.server.Server adapter (Milestone 3)."""


import mcp.types as mcp_types
from mcp.server.lowlevel import Server

from posthog.mcp import instrument
from posthog.test.mcp._helpers import (
    FakeClient,
    events_named as _events,
    flush_background as _flush,
)


def make_server():
    server = Server("test-lowlevel")

    @server.list_tools()
    async def list_tools():
        return [
            mcp_types.Tool(
                name="echo",
                description="Echo back a message",
                inputSchema={
                    "type": "object",
                    "properties": {"msg": {"type": "string"}},
                    "required": ["msg"],
                },
            )
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        if name == "echo":
            return [mcp_types.TextContent(type="text", text=str(arguments.get("msg")))]
        raise ValueError("boom")

    return server


def _call_request(name, arguments):
    return mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
    )


async def test_list_tools_injects_optional_context_and_captures():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    handler = server.request_handlers[mcp_types.ListToolsRequest]
    result = await handler(mcp_types.ListToolsRequest(method="tools/list"))
    await _flush()

    tool = result.root.tools[0]
    assert "context" in tool.inputSchema["properties"]
    # context is OPTIONAL on the low-level path (schema is also the validation schema)
    assert "context" not in tool.inputSchema.get("required", [])

    listed = _events(client, "$mcp_tools_list")
    assert listed and listed[0]["properties"]["$mcp_listed_tool_names"] == ["echo"]


async def test_tool_call_success_captures_intent():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    # populate the tool cache first
    await server.request_handlers[mcp_types.ListToolsRequest](
        mcp_types.ListToolsRequest(method="tools/list")
    )

    handler = server.request_handlers[mcp_types.CallToolRequest]
    result = await handler(
        _call_request(
            "echo", {"msg": "hi", "context": "echoing a message for the test"}
        )
    )
    await _flush()

    assert result.root.isError is False
    calls = _events(client, "$mcp_tool_call")
    assert len(calls) == 1
    props = calls[0]["properties"]
    assert props["$mcp_tool_name"] == "echo"
    assert props["$mcp_intent"] == "echoing a message for the test"
    assert props["$mcp_is_error"] is False
    # context is stripped from captured parameters
    assert "context" not in props["$mcp_parameters"]["request"]["params"]["arguments"]


async def test_tool_call_error_captured_from_is_error_result():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    handler = server.request_handlers[mcp_types.CallToolRequest]
    # "fail" is unlisted -> no validation -> handler raises -> isError result
    result = await handler(
        _call_request("fail", {"context": "trying a tool that errors"})
    )
    await _flush()

    assert result.root.isError is True
    calls = _events(client, "$mcp_tool_call")
    assert calls and calls[0]["properties"]["$mcp_is_error"] is True
    exceptions = _events(client, "$exception")
    assert (
        exceptions
        and "boom" in exceptions[0]["properties"]["$exception_list"][0]["value"]
    )


async def test_initialize_emitted_once():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    handler = server.request_handlers[mcp_types.CallToolRequest]
    await handler(_call_request("echo", {"msg": "a", "context": "first call"}))
    await handler(_call_request("echo", {"msg": "b", "context": "second call"}))
    await _flush()

    assert len(_events(client, "$mcp_initialize")) == 1
    assert len(_events(client, "$mcp_tool_call")) == 2
