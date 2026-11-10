"""Standalone FastMCP turns a raised tool exception into an ``isError`` result
before the handler we wrap returns. The failure must still carry what was raised
(posthog-python#1223)."""

import pytest

pytest.importorskip("fastmcp")

from fastmcp import Client, FastMCP  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

from posthog.mcp import MCPAnalyticsOptions, instrument  # noqa: E402
from posthog.test.mcp._helpers import FakeClient, events_named, flush_background  # noqa: E402


def _upstream():
    raise ConnectionError("upstream down")


def _chained():
    try:
        _upstream()
    except ConnectionError as exc:
        raise RuntimeError("lookup failed") from exc


def _tool_error():
    raise ToolError("bad input")


@pytest.mark.parametrize(
    "raise_in_tool, mask_error_details, error_type, message, chain",
    [
        (_chained, False, "RuntimeError", "lookup failed", ["RuntimeError", "ConnectionError"]),
        (_chained, True, "RuntimeError", "lookup failed", ["RuntimeError", "ConnectionError"]),
        (_tool_error, False, "ToolError", "bad input", ["ToolError"]),
    ],
)
async def test_failed_call_carries_the_raised_exception(
    raise_in_tool, mask_error_details, error_type, message, chain
):
    server = FastMCP("example-server", mask_error_details=mask_error_details)

    @server.tool
    def fail() -> str:
        raise_in_tool()
        return "unreachable"

    sink = FakeClient()
    instrument(server, sink, MCPAnalyticsOptions())

    async with Client(server) as client:
        result = await client.call_tool("fail", {}, raise_on_error=False)
    await flush_background()

    assert result.is_error
    [call] = events_named(sink, "$mcp_tool_call")
    assert call["properties"]["$mcp_is_error"] is True
    assert call["properties"]["$mcp_error_type"] == error_type
    assert call["properties"]["$mcp_error_message"] == message
    [exception] = events_named(sink, "$exception")
    exception_list = exception["properties"]["$exception_list"]
    raised = [e for e in exception_list if e["type"] in chain]
    assert [e["type"] for e in raised] == chain
    assert all(e["stacktrace"]["frames"] for e in raised)


async def test_exception_caught_by_the_tool_is_not_reported():
    server = FastMCP("example-server")

    @server.tool
    def inner() -> str:
        raise RuntimeError("inner failure")

    @server.tool
    async def outer() -> str:
        try:
            await server.call_tool("inner", {})
        except ToolError:
            pass
        return "recovered"

    sink = FakeClient()
    instrument(server, sink, MCPAnalyticsOptions())

    async with Client(server) as client:
        result = await client.call_tool("outer", {}, raise_on_error=False)
    await flush_background()

    assert not result.is_error
    [call] = events_named(sink, "$mcp_tool_call")
    assert call["properties"]["$mcp_is_error"] is False
    assert events_named(sink, "$exception") == []
