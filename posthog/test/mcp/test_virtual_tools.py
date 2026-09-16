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


def _static_list(tools):
    """A ``tools/list`` handler serving one unpaginated page, registered directly
    into ``request_handlers`` the way a raw low-level host does."""

    async def handler(req):
        return mcp_types.ServerResult(mcp_types.ListToolsResult(tools=list(tools)))

    return handler


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
    # Every first page is judged on its own tools, so a host that removes the
    # colliding tool gets the virtual one back without re-instrumenting.
    pages = [[_REAL_GET_MORE_TOOLS]]
    server = _make_paged_lowlevel(pages)
    instrument(server, FakeClient(), MCPAnalyticsOptions(report_missing=True))

    assert _names(await _list_page(server)) == ["get_more_tools"]
    pages[0] = [_ECHO_TOOL]
    assert _names(await _list_page(server)) == ["echo", "get_more_tools"]


async def test_registry_probe_blocks_interception_before_any_listing():
    # The multi-pod case: a call reaching a process that never served a
    # tools/list has nothing to go on but the registry.
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


async def test_the_probe_asks_the_host_once_per_virtual_tool_call():
    # The raw-server probe asks per call rather than trusting a listing: a
    # server serving different catalogues to different callers would otherwise
    # answer one caller from another's listing. @posthog/mcp probes per call for
    # the same reason. Pinning the count keeps that cost visible if the probe
    # ever widens to ordinary tool traffic.
    #
    # Counted inside the host's own handler, which the probe calls directly.
    calls = []
    server = Server("virtual-tools-counted")

    @server.call_tool()
    async def call_tool(name, arguments):
        return [mcp_types.TextContent(type="text", text="real tool ran")]

    async def counted_list(req):
        # `req is None` is the MCP SDK repopulating its own validation cache,
        # which it does on a call to an unlisted name. Not ours, so not counted.
        if req is not None:
            calls.append(req)
        return mcp_types.ServerResult(mcp_types.ListToolsResult(tools=[_ECHO_TOOL]))

    server.request_handlers[mcp_types.ListToolsRequest] = counted_list
    instrument(server, FakeClient(), MCPAnalyticsOptions(report_missing=True))

    await _list_page(server)
    assert len(calls) == 1

    await _call(server, "get_more_tools", {"context": "need csv"})
    await _call(server, "get_more_tools", {"context": "need csv"})
    assert len(calls) == 3  # one probe per virtual-tool call

    # Ordinary tool traffic never reaches the probe.
    await _call(server, "echo", {"msg": "hi"})
    assert len(calls) == 3


async def test_unanswerable_ownership_check_delegates_to_the_host():
    # When the SDK cannot tell whose tool a name is -- here the host's tools/list
    # handler is failing -- it must hand the call to the host rather than
    # intercept it. Guessing the other way swallows a real tool of theirs
    # silently, for as long as the handler stays unwell; guessing this way costs
    # one failed call to a tool of PostHog's, which the agent can see and retry.
    # @posthog/mcp delegates on the same reasoning.
    server = Server("virtual-tools-unanswerable")

    @server.call_tool()
    async def call_tool(name, arguments):
        return [mcp_types.TextContent(type="text", text=f"host dispatched {name}")]

    async def failing_list(req):
        # Fails only for real listing requests, so the MCP SDK's own `req is
        # None` validation-cache pass still works and only the SDK's ownership
        # check is affected.
        if req is not None:
            raise RuntimeError("catalogue backend unavailable")
        return mcp_types.ServerResult(mcp_types.ListToolsResult(tools=[_ECHO_TOOL]))

    server.request_handlers[mcp_types.ListToolsRequest] = failing_list
    client = FakeClient()
    messages = []
    instrument(
        server,
        client,
        MCPAnalyticsOptions(
            report_missing=True, collect_feedback=True, logger=messages.append
        ),
    )

    missing = await _call(server, "get_more_tools", {"context": "need csv"})
    feedback = await _call(server, "send_feedback", {"summary": "s"})
    await _flush()

    assert missing.root.content[0].text == "host dispatched get_more_tools"
    assert feedback.root.content[0].text == "host dispatched send_feedback"
    assert _events(client, "$mcp_missing_capability") == []
    assert _events(client, "$mcp_feedback") == []
    # The host is told why their call was not instrumented.
    assert any("delegating the call to your server" in m for m in messages)


# --- the probe must not corrupt the SDK's validation cache ---------------------


def _make_decorated_lowlevel():
    """A raw low-level server that registers ``tools/list`` through the SDK's own
    decorator and rebuilds its ``Tool`` objects on every call, the way a host
    reading from a database would. The decorator refreshes ``Server._tool_cache``
    from whatever it returns, and that cache is the schema real tool calls are
    validated against -- so anything that runs this handler must re-inject."""
    server = Server("virtual-tools-decorated")

    @server.list_tools()
    async def list_tools():
        return [
            mcp_types.Tool(
                name="echo",
                description="Echo",
                inputSchema={
                    "type": "object",
                    "properties": {"msg": {"type": "string"}},
                    "additionalProperties": False,
                },
            )
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        return [mcp_types.TextContent(type="text", text="real tool ran")]

    return server


async def test_a_virtual_tool_call_leaves_real_tools_callable():
    # Regression: the ownership check ran the host's own list_tools, which made
    # the SDK decorator rebuild `_tool_cache` from un-injected schemas. The next
    # real call carrying the `context` we advertised was then rejected with
    # "Additional properties are not allowed".
    server = _make_decorated_lowlevel()
    instrument(
        server, FakeClient(), MCPAnalyticsOptions(report_missing=True, context=True)
    )

    await _list_page(server)
    before = await _call(server, "echo", {"msg": "hi", "context": "say hi"})
    assert before.root.isError is not True

    await _call(server, "get_more_tools", {"context": "need csv export"})

    after = await _call(server, "echo", {"msg": "hi", "context": "say hi again"})
    await _flush()
    assert after.root.isError is not True, after.root.content[0].text


async def test_a_listing_handler_registered_after_instrument_is_not_swallowed():
    # Regression: the probe closed over the handler captured at instrument time,
    # so a host registering `tools/list` afterwards had ownership answered from a
    # catalogue no client ever saw -- a confident wrong answer that swallowed
    # their real tool. Unknown must delegate instead.
    server = Server("virtual-tools-late")

    @server.call_tool()
    async def call_tool(name, arguments):
        return [mcp_types.TextContent(type="text", text="real tool ran")]

    server.request_handlers[mcp_types.ListToolsRequest] = _static_list([_ECHO_TOOL])
    instrument(server, FakeClient(), MCPAnalyticsOptions(report_missing=True))

    # The host replaces its listing after instrumentation, now owning the name.
    server.request_handlers[mcp_types.ListToolsRequest] = _static_list(
        [_REAL_GET_MORE_TOOLS]
    )

    out = await _call(server, "get_more_tools", {"context": "need csv export"})
    await _flush()
    assert out.root.content[0].text == "real tool ran"


async def test_a_blocked_name_is_not_also_reported_as_shadowed():
    # The host owns the name on page one *and* a later page. Page one blocks
    # injection, so nothing of PostHog's is advertised and the host's tool runs.
    # Warning "the real tool will not run" on page two would send them chasing a
    # bug that isn't there.
    server = _make_paged_lowlevel([[_REAL_GET_MORE_TOOLS], [_REAL_GET_MORE_TOOLS]])
    messages = []
    instrument(
        server,
        FakeClient(),
        MCPAnalyticsOptions(report_missing=True, logger=messages.append),
    )

    await _list_page(server)
    await _list_page(server, cursor="1")

    assert any("Cannot inject" in m for m in messages)
    assert not any("already injected" in m for m in messages)

    out = await _call(server, "get_more_tools", {"context": "need csv export"})
    await _flush()
    assert out.root.content[0].text == "real tool ran"


async def test_a_duplicate_dropped_name_is_not_reported_as_shadowed():
    # Both virtual tools configured to one name: missing-capability wins the
    # first page and feedback is dropped. When the host's own tool by that name
    # turns up on a later page, only the tool we actually injected shadows it --
    # warning that the dropped one does too is a bug the host cannot act on.
    server = _make_paged_lowlevel([[_ECHO_TOOL], [_REAL_GET_MORE_TOOLS]])
    messages = []
    instrument(
        server,
        FakeClient(),
        MCPAnalyticsOptions(
            report_missing=True,
            collect_feedback=CollectFeedbackOptions(tool_name="get_more_tools"),
            logger=messages.append,
        ),
    )

    await _list_page(server)
    await _list_page(server, cursor="1")

    shadowed = [m for m in messages if "already injected" in m]
    assert len(shadowed) == 1
    assert "missing_capability_tool_name" in shadowed[0]


async def test_a_removed_listing_handler_is_not_swallowed():
    # The sibling of a replaced handler: removed outright. Ownership is then
    # unanswerable, so the call must be delegated rather than intercepted, and
    # the host told why -- a silent stop is invisible in the captured data.
    server = Server("virtual-tools-removed")

    @server.call_tool()
    async def call_tool(name, arguments):
        return [mcp_types.TextContent(type="text", text="real tool ran")]

    server.request_handlers[mcp_types.ListToolsRequest] = _static_list([_ECHO_TOOL])
    client = FakeClient()
    messages = []
    instrument(
        server,
        client,
        MCPAnalyticsOptions(report_missing=True, logger=messages.append),
    )

    del server.request_handlers[mcp_types.ListToolsRequest]

    out = await _call(server, "get_more_tools", {"context": "need csv export"})
    await _flush()

    assert out.root.content[0].text == "real tool ran"
    assert _events(client, "$mcp_missing_capability") == []
    assert any("replaced or removed" in m for m in messages)
