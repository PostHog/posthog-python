"""Tests for jlowin's standalone FastMCP 2.0 (the `fastmcp` package), distinct
from the official SDK's mcp.server.fastmcp.FastMCP."""


import pytest

pytest.importorskip("fastmcp")

import mcp.types as mcp_types  # noqa: E402
from fastmcp import FastMCP  # noqa: E402

from posthog.mcp import instrument  # noqa: E402
from posthog.mcp.types import MCPAnalyticsOptions  # noqa: E402
from posthog.test.mcp._helpers import (  # noqa: E402
    FakeClient,
    events_named as _events,
    flush_background as _flush,
)


def make_server():
    server = FastMCP("jlowin-probe")

    @server.tool
    def add(a: int, b: int) -> int:
        return a + b

    return server


async def _list(server):
    handler = server._mcp_server.request_handlers[mcp_types.ListToolsRequest]
    return await handler(mcp_types.ListToolsRequest(method="tools/list"))


async def _call(server, name, arguments):
    handler = server._mcp_server.request_handlers[mcp_types.CallToolRequest]
    return await handler(
        mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
        )
    )


async def test_jlowin_list_injects_context():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    result = await _list(server)
    await _flush()

    add_tool = next(t for t in result.root.tools if t.name == "add")
    assert "context" in add_tool.inputSchema["properties"]
    assert _events(client, "$mcp_tools_list")


async def test_jlowin_call_strips_context_so_validation_passes():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    out = await _call(
        server,
        "add",
        {"a": 2, "b": 3, "context": "summing two numbers for the monthly report"},
    )
    await _flush()

    # Without stripping, jlowin rejects the extra `context` kwarg with isError.
    assert out.root.isError is False
    calls = _events(client, "$mcp_tool_call")
    assert calls and calls[0]["properties"]["$mcp_tool_name"] == "add"
    assert (
        calls[0]["properties"]["$mcp_intent"]
        == "summing two numbers for the monthly report"
    )
    assert calls[0]["properties"]["$mcp_is_error"] is False


async def test_jlowin_report_missing_advertises_get_more_tools():
    server = make_server()
    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(report_missing=True))

    result = await _list(server)
    assert "get_more_tools" in [t.name for t in result.root.tools]

    out = await _call(
        server, "get_more_tools", {"context": "need a tool that exports to CSV"}
    )
    await _flush()
    assert out.root.isError is False
    assert _events(client, "$mcp_missing_capability")
