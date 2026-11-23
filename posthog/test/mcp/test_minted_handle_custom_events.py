"""A custom event captured during a tool call waits until the call settles, then
goes out with the call's own session and identity. Run through the real mcp 1.x
adapters."""

import asyncio
from contextvars import ContextVar

import mcp.types as mcp_types
import pytest
from pydantic import BaseModel, ConfigDict
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_connected_server_and_client_session

from posthog.mcp import McpAnalytics, instrument
from posthog.mcp.types import MCPAnalyticsOptions, UserIdentity
from posthog.mcp._internal import get_server_tracking_data
from posthog.mcp.session import MAX_DEFERRED_CUSTOM_EVENTS
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


@pytest.mark.parametrize("anchored", [True, False], ids=["anchored", "not anchored"])
async def test_a_custom_event_keeps_the_properties_it_was_captured_with(anchored):
    client = FakeClient()
    server = FastMCP("t")
    analytics: list[McpAnalytics] = []

    @server.tool()
    async def tool() -> str:
        properties = {"step": 0}
        await analytics[0].capture("progress", properties)
        properties["step"] = 1
        await analytics[0].capture("progress", properties)
        properties.clear()
        return "ok"

    options = MCPAnalyticsOptions(enable_conversation_id=anchored)
    analytics.append(instrument(server, client, options))
    async with create_connected_server_and_client_session(
        server._mcp_server
    ) as session:
        await session.call_tool("tool", {})
    await flush_background()

    assert [e["properties"].get("step") for e in events_named(client, "progress")] == [
        0,
        1,
    ]


async def test_a_call_sends_at_most_its_bound_of_custom_events():
    client = FakeClient()
    server = FastMCP("t")
    analytics: list[McpAnalytics] = []

    @server.tool()
    async def chatty() -> str:
        for i in range(MAX_DEFERRED_CUSTOM_EVENTS + 5):
            await analytics[0].capture("tick", {"i": i})
        return "ok"

    analytics.append(instrument(server, client, anchored()))
    async with create_connected_server_and_client_session(
        server._mcp_server
    ) as session:
        await session.call_tool("chatty", {})
    await flush_background()

    assert len(events_named(client, "tick")) == MAX_DEFERRED_CUSTOM_EVENTS


consent: ContextVar[str] = ContextVar("consent", default="unset")


async def test_before_send_sees_the_context_the_tool_captured_in():
    client = FakeClient()
    server = FastMCP("t")
    analytics: list[McpAnalytics] = []
    seen: list[str] = []

    def before_send(event):
        if event["event"] == "inside_tool":
            seen.append(consent.get())
        return event

    @server.tool()
    async def tool() -> str:
        token = consent.set("denied")
        try:
            await analytics[0].capture("inside_tool")
        finally:
            consent.reset(token)
        return "ok"

    options = MCPAnalyticsOptions(enable_conversation_id=True, before_send=before_send)
    analytics.append(instrument(server, client, options))
    async with create_connected_server_and_client_session(
        server._mcp_server
    ) as session:
        await session.call_tool("tool", {})
    await flush_background()

    assert seen == ["denied"]


class StrictResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


async def test_a_listing_during_a_call_leaves_its_structured_result_valid():
    """The SDK validates a call's result against the schema it held when the call
    started. A listing from a handle-free client declared no `_mcp_instructions`;
    an ordinary listing that lands while the tool awaits must not add it."""
    client = FakeClient()
    server = FastMCP("t")
    entered, release = asyncio.Event(), asyncio.Event()

    @server.tool()
    async def strict() -> StrictResult:
        entered.set()
        await release.wait()
        return StrictResult(value=7)

    data = get_server_tracking_data(instrument(server, client, anchored())._key)
    data.tool_output_instructions["strict"] = False
    call = asyncio.create_task(
        server._tool_manager.call_tool(
            "strict", {}, context=server.get_context(), convert_result=True
        )
    )
    await entered.wait()
    data.tool_output_instructions["strict"] = True
    release.set()
    result = await call

    assert "_mcp_instructions" not in repr(result)
