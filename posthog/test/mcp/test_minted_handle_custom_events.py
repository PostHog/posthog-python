"""A custom event captured during a tool call waits until the call settles, then
goes out with the call's own session and identity. Run through the real mcp 1.x
adapters."""

import asyncio
from collections.abc import Mapping
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


async def test_a_call_sends_every_custom_event_past_its_bound():
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

    assert len(events_named(client, "tick")) == MAX_DEFERRED_CUSTOM_EVENTS + 5


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


async def test_a_custom_event_keeps_the_options_it_was_captured_with():
    client = FakeClient()
    server = FastMCP("t")
    analytics: list[McpAnalytics] = []

    @server.tool()
    async def tool() -> str:
        options = {"process_person_profile": False}
        await analytics[0].capture("inside_tool", event_options=options)
        options["process_person_profile"] = True
        return "ok"

    options = MCPAnalyticsOptions(
        enable_conversation_id=True, identify=UserIdentity(distinct_id="u1")
    )
    analytics.append(instrument(server, client, options))
    async with create_connected_server_and_client_session(
        server._mcp_server
    ) as session:
        await session.call_tool("tool", {})
    await flush_background()

    (event,) = events_named(client, "inside_tool")
    assert event["options"].get("process_person_profile") is False


class UnreadableProperties(Mapping):
    def __getitem__(self, key):
        raise KeyError(key)

    def __iter__(self):
        raise RuntimeError("cannot iterate")

    def __len__(self):
        return 1


async def test_properties_that_cannot_be_copied_leave_the_call_alone():
    client = FakeClient()
    server = FastMCP("t")
    analytics: list[McpAnalytics] = []

    @server.tool()
    async def tool() -> str:
        await analytics[0].capture("inside_tool", UnreadableProperties())
        return "ok"

    analytics.append(instrument(server, client, anchored()))
    async with create_connected_server_and_client_session(
        server._mcp_server
    ) as session:
        result = await session.call_tool("tool", {})

    assert not result.isError


def session_and_person(event):
    return event["distinct_id"], event["properties"]["$session_id"]


@pytest.mark.parametrize("flush", [False, True], ids=["no flush", "flush mid-call"])
async def test_a_flush_during_a_call_leaves_its_custom_events_to_the_call(flush):
    client = FakeClient()
    server = FastMCP("t")
    analytics: list[McpAnalytics] = []
    captured, release = asyncio.Event(), asyncio.Event()

    @server.tool()
    async def job() -> str:
        await analytics[0].capture("progress")
        captured.set()
        await release.wait()
        return "done"

    options = MCPAnalyticsOptions(
        enable_conversation_id=False, identify=UserIdentity(distinct_id="u1")
    )
    analytics.append(instrument(server, client, options))
    async with create_connected_server_and_client_session(
        server._mcp_server
    ) as session:
        call = asyncio.create_task(session.call_tool("job", {}))
        await captured.wait()
        if flush:
            await analytics[0].flush()
        release.set()
        await call
    await flush_background()

    (tool_call,) = events_named(client, "$mcp_tool_call")
    (custom,) = events_named(client, "progress")
    assert session_and_person(custom) == session_and_person(tool_call)


def counting_identify(calls: list, wait: "asyncio.Event | None" = None):
    async def identify(request, extra):
        calls.append(request)
        if wait is not None and len(calls) == 1:
            await wait.wait()
        return UserIdentity(distinct_id="u1")

    return identify


@pytest.mark.parametrize(
    "ending", ["cancelled", "no tool name", "cancelled while it identifies"]
)
async def test_a_call_that_records_nothing_runs_identify_no_more_than_once(ending):
    client = FakeClient()
    calls: list = []
    hold = asyncio.Event()
    entered = asyncio.Event()

    if ending == "no tool name":
        server = Server("t")

        @server.list_tools()
        async def list_tools():
            return [mcp_types.Tool(name="echo", inputSchema={"type": "object"})]

        @server.call_tool()
        async def call_tool(name, arguments):
            raise ValueError("no tool")

        analytics = instrument(
            server, client, MCPAnalyticsOptions(identify=counting_identify(calls))
        )
        async with create_connected_server_and_client_session(server) as session:
            await session.call_tool("", {})
        await analytics.flush()
        assert len(calls) == 0
        return

    server = FastMCP("t")
    identifies = ending == "cancelled while it identifies"

    @server.tool()
    async def job() -> str:
        if not identifies:
            entered.set()
            await asyncio.sleep(10)
        return "ok"

    identify = counting_identify(calls, hold if identifies else None)
    analytics = instrument(server, client, MCPAnalyticsOptions(identify=identify))
    task = asyncio.create_task(
        server._tool_manager.call_tool(
            "job", {}, context=server.get_context(), convert_result=True
        )
    )
    if identifies:
        while not calls:
            await asyncio.sleep(0)
    else:
        await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await analytics.flush()

    assert len(calls) == (1 if identifies else 0)


async def test_a_flush_sends_a_cancelled_calls_custom_events_through_async_hooks():
    client = FakeClient()
    server = FastMCP("t")
    analytics: list[McpAnalytics] = []
    reached = asyncio.Event()

    async def slow_before_send(event):
        await asyncio.sleep(0.05)
        return event

    async def async_identify(request, extra):
        await asyncio.sleep(0.01)
        return UserIdentity(distinct_id="u1")

    @server.tool()
    async def slow() -> str:
        await analytics[0].capture("inside_tool")
        reached.set()
        await asyncio.sleep(10)
        return "ok"

    options = MCPAnalyticsOptions(
        enable_conversation_id=True,
        identify=async_identify,
        before_send=slow_before_send,
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
    assert len(events_named(client, "inside_tool")) == 1


async def test_a_custom_event_captured_as_its_call_settles_gets_the_settled_session(
    monkeypatch,
):
    """A tool's own thread can capture at the instant its call settles: the session
    it picks and the settlement must not straddle it."""
    import posthog.mcp as mcp_module
    from posthog.mcp.session import (
        PendingCustomEvents,
        ResolvedSession,
        bind_in_tool_session,
        current_session,
    )

    client = FakeClient()
    analytics = instrument(FastMCP("t"), client, anchored())
    data = get_server_tracking_data(analytics._key)
    primed = ResolvedSession("ses_primed", "generated")
    settled = ResolvedSession("ses_settled", "conversation")
    pending = PendingCustomEvents()

    def read_then_settle(read_from):
        session = current_session(read_from)
        pending.settle(settled)
        return session

    monkeypatch.setattr(mcp_module, "current_session", read_then_settle)
    with bind_in_tool_session(data, primed, pending):
        await analytics.capture("inside_tool")
    await analytics.flush()
    await flush_background()

    (custom,) = events_named(client, "inside_tool")
    assert custom["properties"]["$session_id"] == settled.session_id
