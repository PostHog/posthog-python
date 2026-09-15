"""Tests for the ``get_more_tools`` virtual tool (the ``report_missing`` option):
first-page-only injection on a paginated listing, and name collisions with a real
application tool.

``send_feedback``'s equivalents live in ``test_feedback.py``. Both tools share one
kind-keyed resolver in ``_instrumentation``, so a change that breaks one of these
files should break the other.
"""

import mcp.types as mcp_types
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel import Server

from posthog.mcp import CollectFeedbackOptions, instrument
from posthog.mcp.tools import get_more_tools_result_text
from posthog.mcp.types import MCPAnalyticsOptions
from posthog.test.mcp._helpers import (
    FakeClient,
    events_named as _events,
    flush_background as _flush,
)

_REAL_GET_MORE_TOOLS = mcp_types.Tool(
    name="get_more_tools",
    description="A real application tool that owns the name",
    inputSchema={"type": "object", "properties": {"context": {"type": "string"}}},
)
_ECHO_TOOL = mcp_types.Tool(
    name="echo",
    description="Echo",
    inputSchema={"type": "object", "properties": {"msg": {"type": "string"}}},
)


def _make_paged_lowlevel(pages):
    """A raw low-level server whose tools/list handler serves ``pages`` (a list of
    tool lists) one page per request, chained by ``nextCursor``. The paged handler
    is registered directly into ``request_handlers`` so the wire pagination shape
    is exact; the real tool handler answers ``real tool ran``."""
    server = Server("virtual-tools-paged")

    @server.call_tool()
    async def call_tool(name, arguments):
        return [mcp_types.TextContent(type="text", text="real tool ran")]

    async def paged_list(req):
        cursor = getattr(getattr(req, "params", None), "cursor", None) if req else None
        index = int(cursor) if cursor else 0
        next_cursor = str(index + 1) if index + 1 < len(pages) else None
        return mcp_types.ServerResult(
            mcp_types.ListToolsResult(tools=list(pages[index]), nextCursor=next_cursor)
        )

    server.request_handlers[mcp_types.ListToolsRequest] = paged_list
    return server


def _list_page(server, cursor=None):
    """Request one page. ``cursor=None`` is a first page; any string -- including
    ``""``, a valid opaque cursor -- is a continuation."""
    handler = server.request_handlers[mcp_types.ListToolsRequest]
    params = (
        mcp_types.PaginatedRequestParams(cursor=cursor) if cursor is not None else None
    )
    return handler(mcp_types.ListToolsRequest(method="tools/list", params=params))


def _call_request(name, arguments):
    return mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
    )


async def _call(server, name, arguments):
    handler = server.request_handlers[mcp_types.CallToolRequest]
    return await handler(_call_request(name, arguments))


def _names(page):
    return [tool.name for tool in page.root.tools]


# --- pagination ----------------------------------------------------------------


async def test_appended_to_first_page_only():
    # The regression test for the original bug: get_more_tools had no page gate
    # at all, so a cursor-following client saw it once per page.
    server = _make_paged_lowlevel([[_ECHO_TOOL], [_ECHO_TOOL]])
    instrument(server, FakeClient(), MCPAnalyticsOptions(report_missing=True))

    assert _names(await _list_page(server)) == ["echo", "get_more_tools"]
    assert _names(await _list_page(server, cursor="1")) == ["echo"]


async def test_single_unpaginated_listing_still_gets_the_tool():
    server = _make_paged_lowlevel([[_ECHO_TOOL]])
    instrument(server, FakeClient(), MCPAnalyticsOptions(report_missing=True))

    assert _names(await _list_page(server)) == ["echo", "get_more_tools"]


async def test_empty_string_cursor_is_a_continuation_page():
    server = _make_paged_lowlevel([[_ECHO_TOOL]])
    instrument(server, FakeClient(), MCPAnalyticsOptions(report_missing=True))

    assert _names(await _list_page(server, cursor="")) == ["echo"]


async def test_both_virtual_tools_are_appended_to_the_first_page_only():
    # The two tools share one resolver, so they must agree on the page rule.
    server = _make_paged_lowlevel([[_ECHO_TOOL], [_ECHO_TOOL]])
    instrument(
        server,
        FakeClient(),
        MCPAnalyticsOptions(report_missing=True, collect_feedback=True),
    )

    assert _names(await _list_page(server)) == [
        "echo",
        "get_more_tools",
        "send_feedback",
    ]
    assert _names(await _list_page(server, cursor="1")) == ["echo"]


# --- name collisions -----------------------------------------------------------


async def test_first_page_collision_lets_the_real_tool_win():
    # Before this, get_more_tools had no collision handling at all: the host's
    # real tool was silently swallowed and the agent got PostHog's canned reply.
    server = _make_paged_lowlevel([[_REAL_GET_MORE_TOOLS], [_ECHO_TOOL]])
    client = FakeClient()
    messages = []
    instrument(
        server,
        client,
        MCPAnalyticsOptions(report_missing=True, logger=messages.append),
    )

    listed = _names(await _list_page(server)) + _names(
        await _list_page(server, cursor="1")
    )
    assert listed.count("get_more_tools") == 1  # the real tool, never appended

    out = await _call(server, "get_more_tools", {"context": "need csv export"})
    await _flush()

    assert out.root.content[0].text == "real tool ran"
    assert _events(client, "$mcp_missing_capability") == []
    assert _events(client, "$mcp_tool_call")
    assert any("Cannot inject PostHog's" in m for m in messages)
    # The warning has to name the way out, or nobody acts on it.
    assert any("missing_capability_tool_name" in m for m in messages)


async def test_later_page_collision_shadows_the_real_tool():
    # Page one cannot see page two, so the virtual tool is already advertised by
    # the time the real one shows up. PostHog keeps intercepting the name and the
    # real tool is shadowed -- the host's remedy is the rename option, which the
    # warning names. @posthog/mcp behaves the same way; do not make this
    # fail-open without also changing the JS SDK.
    server = _make_paged_lowlevel([[_ECHO_TOOL], [_REAL_GET_MORE_TOOLS]])
    client = FakeClient()
    messages = []
    instrument(
        server,
        client,
        MCPAnalyticsOptions(report_missing=True, logger=messages.append),
    )

    listed = _names(await _list_page(server)) + _names(
        await _list_page(server, cursor="1")
    )
    assert listed.count("get_more_tools") == 2  # PostHog's, then the real one

    out = await _call(server, "get_more_tools", {"context": "need csv export"})
    await _flush()

    assert out.root.content[0].text == get_more_tools_result_text()
    assert _events(client, "$mcp_missing_capability")
    assert any("a later tools/list page advertises a real tool" in m for m in messages)
    assert any("missing_capability_tool_name" in m for m in messages)


async def test_collision_un_shadows_once_the_real_tool_is_dropped():
    # The collision state is rewritten on every first page, not accumulated, so
    # a host that removes the colliding tool gets the virtual one back without
    # re-instrumenting.
    pages = [[_REAL_GET_MORE_TOOLS]]
    server = _make_paged_lowlevel(pages)
    instrument(server, FakeClient(), MCPAnalyticsOptions(report_missing=True))

    assert _names(await _list_page(server)) == ["get_more_tools"]
    pages[0] = [_ECHO_TOOL]
    assert _names(await _list_page(server)) == ["echo", "get_more_tools"]


async def test_registry_probe_blocks_interception_before_any_listing():
    # The multi-pod case: a call reaching a process that never served a
    # tools/list has empty collision state, so the registry is the only signal.
    server = FastMCP("virtual-tools-fastmcp")

    @server.tool()
    def get_more_tools(context: str) -> str:
        return "real tool ran"

    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(report_missing=True))

    out = await server._tool_manager.call_tool(
        "get_more_tools", {"context": "need csv export"}
    )
    await _flush()

    assert "real tool ran" in str(out)
    assert _events(client, "$mcp_missing_capability") == []


async def test_raw_list_probe_blocks_interception_before_any_listing():
    # A raw low-level server has no tool registry, so ownership is settled by
    # asking the host's own tools/list handler.
    server = _make_paged_lowlevel([[_REAL_GET_MORE_TOOLS]])
    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(report_missing=True))

    out = await _call(server, "get_more_tools", {"context": "need csv export"})
    await _flush()

    assert out.root.content[0].text == "real tool ran"
    assert _events(client, "$mcp_missing_capability") == []


async def test_both_virtual_tools_configured_with_the_same_name():
    # Two tools by one name would be advertised twice and dead-letter the
    # feedback path, since every call path checks missing-capability first.
    server = _make_paged_lowlevel([[_ECHO_TOOL]])
    messages = []
    instrument(
        server,
        FakeClient(),
        MCPAnalyticsOptions(
            report_missing=True,
            missing_capability_tool_name="ask_posthog",
            collect_feedback=CollectFeedbackOptions(tool_name="ask_posthog"),
            logger=messages.append,
        ),
    )

    assert _names(await _list_page(server)) == ["echo", "ask_posthog"]
    assert any("both" in m and "ask_posthog" in m for m in messages)


# --- custom name ---------------------------------------------------------------


@pytest.mark.parametrize("enable_conversation_id", [False, True])
async def test_renamed_tool_carries_its_own_intent(enable_conversation_id):
    # The virtual tool states its intent in its own `context` argument, so it
    # gets neither an injected `context` nor a `conversation_id` -- and that has
    # to hold for a renamed tool too. The name used to be hardcoded here, so a
    # renamed tool picked up a `conversation_id` the default-named one never got.
    server = _make_paged_lowlevel([[_ECHO_TOOL]])
    instrument(
        server,
        FakeClient(),
        MCPAnalyticsOptions(
            report_missing=True,
            missing_capability_tool_name="find_tools",
            enable_conversation_id=enable_conversation_id,
        ),
    )

    page = await _list_page(server)
    virtual = [tool for tool in page.root.tools if tool.name == "find_tools"][0]
    assert list(virtual.inputSchema["properties"]) == ["context"]
    assert virtual.inputSchema["required"] == ["context"]


async def test_renamed_tool_is_intercepted_and_the_default_name_is_not():
    server = _make_paged_lowlevel([[_ECHO_TOOL]])
    client = FakeClient()
    instrument(
        server,
        client,
        MCPAnalyticsOptions(
            report_missing=True, missing_capability_tool_name="find_tools"
        ),
    )

    await _list_page(server)
    renamed = await _call(server, "find_tools", {"context": "need csv export"})
    default = await _call(server, "get_more_tools", {"context": "need csv export"})
    await _flush()

    assert renamed.root.content[0].text == get_more_tools_result_text()
    assert default.root.content[0].text == "real tool ran"
    assert len(_events(client, "$mcp_missing_capability")) == 1


async def test_real_tool_named_get_more_tools_keeps_normal_injection():
    # With report_missing off the SDK advertises no such tool, so one by that
    # name is an ordinary application tool: it gets `context` injected and its
    # value captured as $mcp_intent, like any other tool's.
    server = _make_paged_lowlevel([[_REAL_GET_MORE_TOOLS]])
    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(report_missing=False, context=True))

    await _list_page(server)
    out = await _call(server, "get_more_tools", {"context": "delete a cohort"})
    await _flush()

    assert out.root.content[0].text == "real tool ran"
    calls = _events(client, "$mcp_tool_call")
    assert calls
    assert calls[0]["properties"]["$mcp_intent"] == "delete a cohort"


# --- a host that reuses one result object --------------------------------------


def _make_cached_lowlevel(tools):
    """A raw low-level server that returns the SAME ``ServerResult`` object from
    every tools/list -- a module-level constant or the host's own cache. The
    appends mutate that object in place, so the SDK must not later read its own
    injected tool back out of it as if the host owned the name."""
    cached = mcp_types.ServerResult(mcp_types.ListToolsResult(tools=list(tools)))
    server = Server("virtual-tools-cached")

    @server.call_tool()
    async def call_tool(name, arguments):
        if name != "echo":
            raise ValueError(f"Unknown tool: {name}")
        return [mcp_types.TextContent(type="text", text="real tool ran")]

    async def list_tools(req):
        return cached

    server.request_handlers[mcp_types.ListToolsRequest] = list_tools
    return server


async def test_cached_result_object_does_not_collide_with_itself():
    # Regression: the SDK saw its own injected tool in the host's reused result
    # object, reported a collision against itself, stopped intercepting, and the
    # agent got an unknown-tool error instead of its feedback being recorded.
    server = _make_cached_lowlevel([_ECHO_TOOL])
    client = FakeClient()
    messages = []
    instrument(
        server,
        client,
        MCPAnalyticsOptions(
            report_missing=True, collect_feedback=True, logger=messages.append
        ),
    )

    await _list_page(server)
    feedback = await _call(server, "send_feedback", {"summary": "hi"})
    missing = await _call(server, "get_more_tools", {"context": "need csv"})
    await _flush()

    assert feedback.root.isError is not True
    assert missing.root.isError is not True
    assert _events(client, "$mcp_feedback")
    assert _events(client, "$mcp_missing_capability")
    assert not [m for m in messages if "Cannot inject PostHog's" in m]


async def test_cached_result_object_is_not_appended_to_twice():
    # The same contamination would also read as a collision on the second
    # listing, so the virtual tools would silently stop being advertised.
    server = _make_cached_lowlevel([_ECHO_TOOL])
    instrument(
        server,
        FakeClient(),
        MCPAnalyticsOptions(report_missing=True, collect_feedback=True),
    )

    first = _names(await _list_page(server))
    second = _names(await _list_page(server))
    assert first.count("get_more_tools") == 1
    assert first.count("send_feedback") == 1
    assert second == first


async def test_listing_stops_the_call_path_reprobing_the_host():
    # Once a listing has been served, its collision state is the answer, so the
    # host's own tools/list handler is left alone on the call path.
    server = _make_paged_lowlevel([[_ECHO_TOOL]])
    instrument(server, FakeClient(), MCPAnalyticsOptions(report_missing=True))

    original = server.request_handlers[mcp_types.ListToolsRequest]
    await _list_page(server)

    calls = []

    async def counting_list(req):
        calls.append(req)
        return await original(req)

    server.request_handlers[mcp_types.ListToolsRequest] = counting_list
    await _call(server, "get_more_tools", {"context": "need csv"})
    assert calls == []
