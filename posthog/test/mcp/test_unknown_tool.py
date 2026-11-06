"""``$mcp_unknown_tool``: a call naming a tool the server never registered is
not a tool call. Runs under both MCP SDK majors over an in-process client, so
the response compared against an uninstrumented twin is the one an agent gets."""

import importlib.util
from contextlib import asynccontextmanager

import mcp.types as mcp_types
import pytest

from posthog.mcp import PostHogMCP, instrument
from posthog.mcp.types import MCPAnalyticsOptions
from posthog.test.mcp._helpers import (
    MCP_MAJOR,
    FakeClient,
    events_named,
    flush_background,
)

_needs_standalone = pytest.mark.skipif(
    importlib.util.find_spec("fastmcp") is None, reason="standalone fastmcp"
)


def _high_level():
    if MCP_MAJOR >= 2:
        from mcp.server.mcpserver import MCPServer as HighLevel
    else:
        from mcp.server.fastmcp import FastMCP as HighLevel
    server = HighLevel("unknown-tool")

    @server.tool()
    def search_docs(query: str) -> str:
        return f"found {query}"

    return server


def _standalone():
    from fastmcp import FastMCP

    server = FastMCP("unknown-tool")

    @server.tool
    def search_docs(query: str) -> str:
        return f"found {query}"

    @server.tool
    def retired(query: str) -> str:
        return query

    server.disable(names={"retired"})
    return server


def _raw(pages):
    """A raw low-level server whose own dispatcher rejects names it does not
    know. ``pages`` are the tools/list pages of tool names, chained by cursor;
    ``None`` registers no tools/list handler at all."""
    from mcp.server.lowlevel import Server

    registered = {name for page in pages or [] for name in page}
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}

    def page_at(cursor):
        index = int(cursor) if cursor else 0
        tools = [
            mcp_types.Tool(
                name=name,
                **{"input_schema" if MCP_MAJOR >= 2 else "inputSchema": schema},
            )
            for name in pages[index]
        ]
        next_cursor = str(index + 1) if index + 1 < len(pages) else None
        if MCP_MAJOR >= 2:
            return mcp_types.ListToolsResult(tools=tools, next_cursor=next_cursor)
        return mcp_types.ListToolsResult(tools=tools, nextCursor=next_cursor)

    def reply(name):
        if name not in registered:
            raise ValueError(f"Unknown tool: {name}")
        return [mcp_types.TextContent(type="text", text="ok")]

    if MCP_MAJOR >= 2:

        async def on_call_tool(ctx, params):
            return mcp_types.CallToolResult(content=reply(params.name))

        async def on_list_tools(ctx, params):
            return page_at(getattr(params, "cursor", None))

        handlers = {"on_call_tool": on_call_tool}
        if pages is not None:
            handlers["on_list_tools"] = on_list_tools
        return Server("unknown-tool", **handlers)

    server = Server("unknown-tool")

    @server.call_tool()
    async def call_tool(name, arguments):
        return reply(name)

    if pages is not None:

        async def list_tools(req):
            params = getattr(req, "params", None) if req else None
            return mcp_types.ServerResult(page_at(getattr(params, "cursor", None)))

        server.request_handlers[mcp_types.ListToolsRequest] = list_tools
    return server


@asynccontextmanager
async def _connected(server):
    if type(server).__module__.startswith("fastmcp"):
        from fastmcp import Client

        async with Client(server) as client:
            yield lambda name, args: client.call_tool(name, args, raise_on_error=False)
    elif MCP_MAJOR >= 2:
        from mcp import Client

        async with Client(server) as client:
            yield client.call_tool
    else:
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(server) as session:
            yield session.call_tool


async def _response(server, name, arguments):
    async with _connected(server) as call:
        try:
            result = await call(name, arguments)
        except Exception as error:  # noqa: BLE001 - the error is the response
            return type(error).__name__, str(error)
    is_error = getattr(result, "is_error", getattr(result, "isError", None))
    return is_error, [getattr(block, "text", None) for block in result.content]


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(_high_level, id="high-level"),
        pytest.param(_standalone, id="standalone-fastmcp", marks=_needs_standalone),
        pytest.param(lambda: _raw([["search_docs"]]), id="raw-low-level"),
    ],
)
async def test_a_call_to_a_tool_that_does_not_exist_is_recorded_as_unknown(build):
    expected = await _response(build(), "no_such_tool", {})
    client = FakeClient()
    server = build()
    instrument(server, client)

    assert await _response(server, "no_such_tool", {}) == expected
    await flush_background()

    [unknown] = events_named(client, "$mcp_unknown_tool")
    assert unknown["properties"]["$mcp_tool_name"] == "no_such_tool"
    assert unknown["properties"]["$session_id"]
    assert events_named(client, "$mcp_tool_call") == []
    assert events_named(client, "$exception") == []


@pytest.mark.parametrize(
    "build, name, arguments",
    [
        pytest.param(_high_level, "search_docs", {}, id="invalid-arguments"),
        pytest.param(
            _standalone,
            "retired",
            {"query": "q"},
            id="disabled-standalone-tool",
            marks=_needs_standalone,
        ),
        pytest.param(
            lambda: _raw(None), "no_such_tool", {}, id="raw-low-level-without-listing"
        ),
        pytest.param(
            lambda: _raw([["search_docs"], ["export"]]),
            "no_such_tool",
            {},
            id="raw-low-level-with-more-pages",
        ),
    ],
)
async def test_a_call_the_sdk_cannot_rule_out_is_a_failed_tool_call(
    build, name, arguments
):
    expected = await _response(build(), name, arguments)
    client = FakeClient()
    server = build()
    instrument(server, client, MCPAnalyticsOptions(enable_conversation_id=False))

    assert await _response(server, name, arguments) == expected
    await flush_background()

    [call] = events_named(client, "$mcp_tool_call")
    assert call["properties"]["$mcp_tool_name"] == name
    assert call["properties"]["$mcp_is_error"] is True
    assert events_named(client, "$mcp_unknown_tool") == []


async def test_a_custom_dispatcher_captures_an_unknown_tool():
    posthog = PostHogMCP("phc_test")
    captured = []
    posthog.capture = lambda event, **kw: captured.append({"event": event, **kw})

    posthog.capture_unknown_tool("export for jane@example.com", session_id="ses_1")
    posthog.flush()

    [unknown] = events_named(captured, "$mcp_unknown_tool")
    assert unknown["properties"]["$mcp_tool_name"] == "export for [redacted]"
    assert unknown["properties"]["$session_id"] == "ses_1"
    assert "$mcp_resource_name" not in unknown["properties"]
    assert events_named(captured, "$mcp_tool_call") == []
