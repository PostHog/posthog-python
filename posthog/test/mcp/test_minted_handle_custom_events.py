"""A custom event captured on a handle minted for its call waits until the call
settles. These are the ways a call can end without the usual result or error
recording, run through the real mcp 1.x adapters."""

import asyncio

import mcp.types as mcp_types
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_connected_server_and_client_session

from posthog.mcp import McpAnalytics, instrument
from posthog.mcp.types import MCPAnalyticsOptions, UserIdentity
from posthog.test.mcp._helpers import FakeClient, events_named, flush_background


def anchored():
    return MCPAnalyticsOptions(enable_conversation_id=True)


@pytest.mark.parametrize(
    "cancelled", ["in the tool", "while the call records"], ids=lambda c: c
)
async def test_a_cancelled_call_still_sends_its_custom_event(cancelled):
    client = FakeClient()
    server = FastMCP("t")
    analytics: list[McpAnalytics] = []
    reached = asyncio.Event()

    async def identify(request, extra):
        reached.set()
        await asyncio.sleep(10)
        return UserIdentity(distinct_id="u1")

    @server.tool()
    async def slow() -> str:
        await analytics[0].capture("inside_tool")
        if cancelled == "in the tool":
            reached.set()
            await asyncio.sleep(10)
        return "x"

    options = MCPAnalyticsOptions(
        enable_conversation_id=True,
        identify=identify if cancelled == "while the call records" else None,
    )
    analytics.append(instrument(server, client, options))
    task = asyncio.create_task(
        server._tool_manager.call_tool(
            "slow", {}, context=server.get_context(), convert_result=True
        )
    )
    await reached.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await analytics[0].flush()
    await flush_background()

    assert events_named(client, "inside_tool")


async def test_a_call_with_no_tool_name_still_sends_its_custom_event():
    client = FakeClient()
    server = Server("t")
    analytics: list[McpAnalytics] = []

    @server.list_tools()
    async def list_tools():
        return [mcp_types.Tool(name="echo", inputSchema={"type": "object"})]

    @server.call_tool()
    async def call_tool(name, arguments):
        await analytics[0].capture("inside_tool")
        raise ValueError(f"no tool {name!r}")

    analytics.append(instrument(server, client, anchored()))
    async with create_connected_server_and_client_session(server) as session:
        await session.call_tool("", {})
    await flush_background()

    assert events_named(client, "inside_tool")


async def test_a_custom_event_that_cannot_be_built_leaves_the_call_alone():
    client = FakeClient()
    server = FastMCP("t")
    analytics: list[McpAnalytics] = []

    @server.tool()
    async def tool() -> str:
        await analytics[0].capture("inside_tool", ["not", "a", "dict"])  # type: ignore[arg-type]
        return "ok"

    analytics.append(instrument(server, client, anchored()))
    async with create_connected_server_and_client_session(
        server._mcp_server
    ) as session:
        result = await session.call_tool("tool", {})
    await flush_background()

    assert not result.isError
    assert events_named(client, "$mcp_tool_call")


async def test_a_held_back_custom_event_keeps_the_duration_it_had_when_captured():
    client = FakeClient()
    server = FastMCP("t")
    analytics: list[McpAnalytics] = []

    @server.tool()
    async def tool() -> str:
        await analytics[0].capture("inside_tool")
        await asyncio.sleep(0.2)
        return "ok"

    analytics.append(instrument(server, client, anchored()))
    async with create_connected_server_and_client_session(
        server._mcp_server
    ) as session:
        await session.call_tool("tool", {})
    await flush_background()

    (event,) = events_named(client, "inside_tool")
    assert event["properties"].get("$mcp_duration_ms", 0) < 50
