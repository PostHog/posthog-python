"""``$mcp_unknown_tool``: a call naming a tool the server never registered is
not a tool call. Runs under both MCP SDK majors over an in-process client, so
the response compared against an uninstrumented twin is the one an agent gets."""

import importlib
import importlib.util
import json
from contextlib import asynccontextmanager

import mcp.types as mcp_types
import pytest

from posthog.mcp import instrument
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


def _fastmcp_can_disable() -> bool:
    try:
        from fastmcp import FastMCP
    except ImportError:
        return False
    return hasattr(FastMCP, "disable")


_needs_server_disable = pytest.mark.skipif(
    not _fastmcp_can_disable(), reason="FastMCP.disable and providers, fastmcp 3+"
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


def _standalone(*middleware):
    """``middleware`` names stock fastmcp middleware as ``"<module>.<class>"``
    under ``fastmcp.server.middleware``."""
    from fastmcp import FastMCP

    server = FastMCP("unknown-tool")

    @server.tool
    def search_docs(query: str) -> str:
        return f"found {query}"

    for path in middleware:
        module, name = path.rsplit(".", 1)
        stock = importlib.import_module(f"fastmcp.server.middleware.{module}")
        server.add_middleware(getattr(stock, name)())
    return server


def _standalone_with_disabled_tool():
    server = _standalone()

    @server.tool
    def retired(query: str) -> str:
        return query

    server.disable(names={"retired"})
    return server


def _standalone_with_application_middleware():
    from fastmcp.server.middleware.logging import LoggingMiddleware

    class AuditMiddleware(LoggingMiddleware):
        async def on_call_tool(self, context, call_next):
            return await call_next(context)

    server = _standalone()
    server.add_middleware(AuditMiddleware())
    return server


def _mounted():
    """A parent serving a sub-server's tools under the ``ns`` namespace, one
    of them disabled on the sub-server."""
    from fastmcp import FastMCP

    sub = FastMCP("sub")

    @sub.tool
    def retired(query: str) -> str:
        return query

    sub.disable(names={"retired"})
    parent = _standalone()
    parent.mount(sub, namespace="ns")
    return parent


def _raw(listed):
    """A raw low-level server whose own dispatcher rejects names it does not
    know and advertises ``listed`` on tools/list."""
    from mcp.server.lowlevel import Server

    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    listing = mcp_types.ListToolsResult(
        tools=[
            mcp_types.Tool(
                name=name,
                **{"input_schema" if MCP_MAJOR >= 2 else "inputSchema": schema},
            )
            for name in listed
        ]
    )

    def reply(name):
        if name not in listed:
            raise ValueError(f"Unknown tool: {name}")
        return [mcp_types.TextContent(type="text", text="ok")]

    if MCP_MAJOR >= 2:

        async def on_call_tool(ctx, params):
            return mcp_types.CallToolResult(content=reply(params.name))

        async def on_list_tools(ctx, params):
            return listing

        return Server(
            "unknown-tool", on_call_tool=on_call_tool, on_list_tools=on_list_tools
        )

    server = Server("unknown-tool")

    @server.call_tool()
    async def call_tool(name, arguments):
        return reply(name)

    async def list_tools(req):
        return mcp_types.ServerResult(listing)

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
    is_error = result.is_error if hasattr(result, "is_error") else result.isError
    return is_error, [getattr(block, "text", None) for block in result.content]


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(_high_level, id="high-level"),
        pytest.param(_standalone, id="standalone-fastmcp", marks=_needs_standalone),
        *(
            pytest.param(
                lambda path=path: _standalone(path),
                id=f"standalone-fastmcp-with-{path.split('.')[0]}-middleware",
                marks=_needs_standalone,
            )
            for path in (
                "logging.LoggingMiddleware",
                "timing.TimingMiddleware",
                "error_handling.ErrorHandlingMiddleware",
            )
        ),
    ],
)
async def test_a_call_to_a_tool_that_does_not_exist_is_recorded_as_unknown(build):
    expected = await _response(build(), "no_such_tool", {})
    client = FakeClient()
    server = build()
    instrument(server, client, MCPAnalyticsOptions(enable_conversation_id=False))

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
            _standalone_with_disabled_tool,
            "retired",
            {"query": "q"},
            id="disabled-standalone-tool",
            marks=_needs_server_disable,
        ),
        pytest.param(
            _mounted,
            "ns_retired",
            {"query": "q"},
            id="disabled-tool-on-mounted-server",
            marks=_needs_server_disable,
        ),
        pytest.param(
            _mounted,
            "ns_no_such_tool",
            {},
            id="server-with-mounted-provider",
            marks=_needs_server_disable,
        ),
        pytest.param(
            _standalone_with_application_middleware,
            "no_such_tool",
            {},
            id="standalone-with-application-middleware",
            marks=_needs_standalone,
        ),
        pytest.param(
            lambda: _raw(["search_docs"]),
            "no_such_tool",
            {},
            id="raw-low-level-unknown-name",
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


_SHAPES = [
    pytest.param(_high_level, id="high-level"),
    pytest.param(_standalone, id="standalone-fastmcp", marks=_needs_standalone),
]


@pytest.mark.parametrize(
    "build", [*_SHAPES, pytest.param(lambda: _raw(["search_docs"]), id="raw-low-level")]
)
async def test_a_call_naming_no_tool_emits_nothing(build):
    expected = await _response(build(), "", {})
    client = FakeClient()
    server = build()
    instrument(server, client, MCPAnalyticsOptions(enable_conversation_id=False))

    assert await _response(server, "", {}) == expected
    await flush_background()

    assert [e["event"] for e in client.events if e["event"] != "$mcp_initialize"] == []


@pytest.mark.parametrize("build", _SHAPES)
async def test_an_unknown_tool_name_is_cut_to_256_characters(build):
    client = FakeClient()
    server = build()
    instrument(server, client, MCPAnalyticsOptions(enable_conversation_id=False))

    await _response(server, "x" * 5000, {})
    await flush_background()

    [unknown] = events_named(client, "$mcp_unknown_tool")
    name = unknown["properties"]["$mcp_tool_name"]
    assert len(name) == 256
    assert name.startswith("x" * 200)


@pytest.mark.parametrize(
    "build, delivers_handle",
    [
        pytest.param(
            _standalone, True, id="standalone-error-result", marks=_needs_standalone
        ),
        pytest.param(_high_level, False, id="high-level-raise"),
    ],
)
async def test_an_unknown_tool_result_carries_the_conversation_handle_it_delivers(
    build, delivers_handle
):
    client = FakeClient()
    server = build()
    instrument(server, client)

    _, texts = await _response(server, "no_such_tool", {})
    await flush_background()

    handles = [
        json.loads(text)["conversation_id"]
        for text in texts
        if text and text.startswith('{"conversation_id"')
    ]
    [unknown] = events_named(client, "$mcp_unknown_tool")
    assert len(handles) == int(delivers_handle)
    assert unknown["properties"].get("$mcp_conversation_id") == next(
        iter(handles), None
    )
