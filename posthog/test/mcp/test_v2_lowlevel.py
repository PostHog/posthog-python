"""End-to-end tests for the MCP Python SDK v2 low-level ``Server`` adapter.

v2 replaced the public ``request_handlers`` dict (keyed by request class) with
constructor-injected handlers stored in ``_request_handlers`` (keyed by method
string) behind ``add_request_handler``/``get_request_handler``. Handlers take
``(ctx, params)`` and raise through to JSON-RPC errors instead of auto-
converting to ``is_error`` results.
"""

import json

import pytest

import mcp.types as mcp_types
from mcp.server.lowlevel import Server

from posthog.mcp import instrument
from posthog.mcp.tools import get_more_tools_result_text
from posthog.mcp.types import MCPAnalyticsOptions
from posthog.test.mcp._helpers import (
    FakeClient,
    events_named as _events,
    flush_background as _flush,
    listed_uris,
)
from posthog.test.mcp._helpers_v2 import fake_ctx


def make_server(*, resource_error: bool = False, listing: str = "resources") -> Server:
    async def on_call_tool(ctx, params):
        if params.name == "boom":
            raise ValueError("explode")
        if params.name == "soft-fail":
            return mcp_types.CallToolResult(
                content=[mcp_types.TextContent(type="text", text="failed politely")],
                is_error=True,
            )
        args = params.arguments or {}
        return mcp_types.CallToolResult(
            content=[
                mcp_types.TextContent(
                    type="text", text=str(args.get("a", 0) + args.get("b", 0))
                )
            ]
        )

    async def on_list_tools(ctx, params):
        return mcp_types.ListToolsResult(
            tools=[
                mcp_types.Tool(
                    name="add",
                    description="Add two numbers",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "a": {"type": "integer"},
                            "b": {"type": "integer"},
                        },
                        "required": ["a", "b"],
                    },
                )
            ]
        )

    server = Server(
        "test-low-v2",
        version="1.2.3",
        on_call_tool=on_call_tool,
        on_list_tools=on_list_tools,
    )

    async def on_list_resources(ctx, params):
        if listing == "error":
            raise ValueError("listing unavailable")
        return mcp_types.ListResourcesResult(
            resources=[]
            if listing == "empty"
            else [
                mcp_types.Resource(
                    name="Guide", uri="file:///guide.md", mime_type="text/markdown"
                )
            ]
        )

    async def on_list_resource_templates(ctx, params):
        return mcp_types.ListResourceTemplatesResult(
            resource_templates=[
                mcp_types.ResourceTemplate(
                    name="Profile",
                    uri_template="users://{user_id}/profile",
                    mime_type="text/markdown",
                )
            ]
        )

    async def on_read_resource(ctx, params):
        if resource_error:
            raise ValueError(f"Cannot read {params.uri}")
        return mcp_types.ReadResourceResult(
            contents=[
                mcp_types.TextResourceContents(
                    uri=params.uri,
                    mime_type="text/markdown",
                    text="# Guide",
                )
            ]
        )

    server.add_request_handler(
        "resources/list", mcp_types.PaginatedRequestParams, on_list_resources
    )
    server.add_request_handler(
        "resources/templates/list",
        mcp_types.PaginatedRequestParams,
        on_list_resource_templates,
    )
    server.add_request_handler(
        "resources/read", mcp_types.ReadResourceRequestParams, on_read_resource
    )
    return server


async def _call_tool(server, name, arguments, ctx=None):
    entry = server.get_request_handler("tools/call")
    params = mcp_types.CallToolRequestParams(name=name, arguments=arguments)
    return await entry.handler(ctx or fake_ctx(), params)


async def _list_tools(server, ctx=None):
    entry = server.get_request_handler("tools/list")
    return await entry.handler(ctx or fake_ctx(method="tools/list"), None)


async def _resource_request(server, method, params=None, ctx=None):
    entry = server.get_request_handler(method)
    return await entry.handler(ctx or fake_ctx(method=method), params)


# --- tools/list --------------------------------------------------------------


async def test_list_tools_injects_optional_context_and_captures():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    result = await _list_tools(server)
    await _flush()

    add_tool = next(t for t in result.tools if t.name == "add")
    # optional on the raw low-level path: this schema is also the call's
    # validation schema, so `context` must not become required
    assert "context" in add_tool.input_schema["properties"]
    assert "context" not in add_tool.input_schema.get("required", [])

    listed = _events(client, "$mcp_tools_list")
    assert listed
    assert listed[0]["properties"]["$mcp_listed_tool_names"] == ["add"]
    assert listed[0]["properties"]["$mcp_server_name"] == "test-low-v2"


@pytest.mark.parametrize(
    "uri, captured_uri, resource_error",
    [
        ("file:///guide.md", "file:///guide.md", False),
        (
            "https://fakeuser:fakepass@example.com/guide",
            "https://%5Bredacted%5D@example.com/guide",
            False,
        ),
        (
            "https://fakeuser:fakepass@example.com/guide",
            "https://%5Bredacted%5D@example.com/guide",
            True,
        ),
        (
            "https://example.com/guide?token=fakesecret&chapter=intro",
            "https://example.com/guide?token=%5Bredacted%5D&chapter=intro",
            False,
        ),
        (
            "https://example.com/guide?token=fakesecret&chapter=intro",
            "https://example.com/guide?token=%5Bredacted%5D&chapter=intro",
            True,
        ),
        (
            "https://example.com/guide?access_token=fakeaccess&X-Amz-Credential=fakecredential&X-Amz-Signature=fakesignature",
            "https://example.com/guide?access_token=%5Bredacted%5D&X-Amz-Credential=%5Bredacted%5D&X-Amz-Signature=%5Bredacted%5D",
            False,
        ),
        (
            "https://example.com/guide?access_token=fakeaccess&X-Amz-Credential=fakecredential&X-Amz-Signature=fakesignature",
            "https://example.com/guide?access_token=%5Bredacted%5D&X-Amz-Credential=%5Bredacted%5D&X-Amz-Signature=%5Bredacted%5D",
            True,
        ),
        (
            "ui://guide/page?%74oken=fakesecret&TOKEN=fakeaccess&chapter=intro#section",
            "ui://guide/page?token=%5Bredacted%5D&TOKEN=%5Bredacted%5D&chapter=intro#section",
            False,
        ),
        (
            "ui://guide/page?%74oken=fakesecret&TOKEN=fakeaccess&chapter=intro#section",
            "ui://guide/page?token=%5Bredacted%5D&TOKEN=%5Bredacted%5D&chapter=intro#section",
            True,
        ),
        # The PostHog-token pass runs before the URL is parsed, so the token is
        # already `[redacted]` by then and the URL rewrite finds nothing left to
        # change — the value keeps that literal form instead of being re-encoded.
        (
            "https://example.com/guide?token=phx_EXAMPLEONLYFAKEVALUE00000000000",
            "https://example.com/guide?token=[redacted]",
            False,
        ),
        (
            "https://example.com/guide?token=phx_EXAMPLEONLYFAKEVALUE00000000000",
            "https://example.com/guide?token=[redacted]",
            True,
        ),
        # An MCP resource uri need not have an authority.
        (
            "resource:guide?token=fakesecret",
            "resource:guide?token=%5Bredacted%5D",
            False,
        ),
    ],
)
async def test_resource_discovery_and_read_are_captured(
    uri: str, captured_uri: str, resource_error: bool
) -> None:
    server = make_server(resource_error=resource_error)
    client = FakeClient()
    instrument(server, client)

    await _resource_request(server, "resources/list")
    read = _resource_request(
        server, "resources/read", mcp_types.ReadResourceRequestParams(uri=uri)
    )
    if resource_error:
        with pytest.raises(ValueError) as caught:
            await read
        assert str(caught.value) == f"Cannot read {uri}"
    else:
        result = await read
        assert result.contents[0].text == "# Guide"
        assert str(result.contents[0].uri) == uri
    await _flush()

    assert len(_events(client, "$mcp_resources_list")) == 1
    reads = _events(client, "$mcp_resource_read")
    assert len(reads) == 1
    props = reads[0]["properties"]
    assert props["$mcp_resource_name"] == captured_uri
    assert props["$mcp_parameters"]["request"]["params"]["uri"] == captured_uri
    assert props["$mcp_is_error"] is resource_error
    assert "$mcp_response" not in props
    exceptions = _events(client, "$exception")
    assert len(exceptions) == int(resource_error)
    if resource_error:
        assert exceptions[0]["properties"]["$mcp_resource_name"] == captured_uri
    for secret in (
        "phx_EXAMPLEONLYFAKEVALUE00000000000",
        "fakeuser",
        "fakepass",
        "fakesecret",
        "fakeaccess",
        "fakecredential",
        "fakesignature",
    ):
        assert secret not in json.dumps(client.events)
    assert props["$mcp_protocol_version"] == "2026-07-28"


@pytest.mark.parametrize(
    "method, listing, listed",
    [
        ("resources/list", "resources", ["file:///guide.md"]),
        ("resources/list", "empty", []),
        ("resources/templates/list", "resources", ["users://{user_id}/profile"]),
    ],
)
async def test_resource_listing_event_carries_the_listing(
    method: str, listing: str, listed: list
) -> None:
    server = make_server(listing=listing)
    client = FakeClient()
    instrument(server, client)

    await _resource_request(server, method)
    await _flush()

    events = _events(client, "$mcp_resources_list")
    assert len(events) == 1
    props = events[0]["properties"]
    assert props["$mcp_parameters"]["request"]["method"] == method
    assert listed_uris(props["$mcp_response"]) == listed
    # An empty listing is a legitimate answer (a template-only server lists no
    # static resources), unlike an empty tools/list.
    assert props["$mcp_is_error"] is False
    assert props["$mcp_duration_ms"] >= 0
    assert "$mcp_resource_name" not in props
    assert not _events(client, "$exception")


async def test_failed_resource_listing_is_captured() -> None:
    server = make_server(listing="error")
    client = FakeClient()
    instrument(server, client)

    with pytest.raises(ValueError, match="listing unavailable"):
        await _resource_request(server, "resources/list")
    await _flush()

    events = _events(client, "$mcp_resources_list")
    assert len(events) == 1
    props = events[0]["properties"]
    assert props["$mcp_is_error"] is True
    assert props["$mcp_duration_ms"] >= 0
    assert "$mcp_response" not in props
    assert "$mcp_resource_name" not in props
    exceptions = _events(client, "$exception")
    assert len(exceptions) == 1
    assert "listing unavailable" in json.dumps(exceptions[0]["properties"])


# --- tools/call --------------------------------------------------------------


async def test_tool_call_captured_with_client_identity():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    # Prime the listing metadata used by the shared call lifecycle.
    await _list_tools(server)
    result = await _call_tool(
        server, "add", {"a": 2, "b": 3, "context": "adding for a report"}
    )
    await _flush()

    assert result.content[0].text == "5"

    calls = _events(client, "$mcp_tool_call")
    assert len(calls) == 1
    props = calls[0]["properties"]
    assert props["$mcp_tool_name"] == "add"
    assert props["$mcp_tool_description"] == "Add two numbers"
    assert props["$mcp_intent"] == "adding for a report"
    assert props["$mcp_is_error"] is False
    assert props["$mcp_client_name"] == "test-client"
    assert props["$mcp_protocol_version"] == "2026-07-28"


async def test_context_left_in_arguments_for_raw_handlers():
    """On the raw low-level path injected keys are NOT stripped — the schema
    advertises them as optional and a ``(name, arguments)`` handler ignores
    extra keys."""
    seen = {}

    async def on_call_tool(ctx, params):
        seen["arguments"] = dict(params.arguments or {})
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="ok")]
        )

    server = Server("raw", on_call_tool=on_call_tool)
    client = FakeClient()
    instrument(server, client)

    await _call_tool(
        server, "anything", {"x": 1, "context": "raw handlers see everything"}
    )
    await _flush()

    assert seen["arguments"] == {"x": 1, "context": "raw handlers see everything"}


async def test_raised_error_is_captured_and_reraised():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    with pytest.raises(ValueError):
        await _call_tool(server, "boom", {"context": "attempting the risky operation"})
    await _flush()

    calls = _events(client, "$mcp_tool_call")
    assert calls and calls[0]["properties"]["$mcp_is_error"] is True
    exceptions = _events(client, "$exception")
    assert exceptions
    assert exceptions[0]["properties"]["$exception_list"][-1]["value"] == "explode"


async def test_is_error_result_is_captured():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    result = await _call_tool(
        server, "soft-fail", {"context": "expecting a polite failure"}
    )
    await _flush()

    assert result.is_error is True
    calls = _events(client, "$mcp_tool_call")
    assert calls and calls[0]["properties"]["$mcp_is_error"] is True


async def test_late_registration_is_wrapped():
    """Handlers registered via ``add_request_handler`` *after* ``instrument()``
    must still be wrapped (the JS #4449 lesson: adapters that hand over a bare
    server and register handlers afterwards)."""
    server = Server("late-reg")  # no handlers yet
    client = FakeClient()
    instrument(server, client)

    async def late_call_tool(ctx, params):
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="late ok")]
        )

    server.add_request_handler(
        "tools/call", mcp_types.CallToolRequestParams, late_call_tool
    )

    async def late_read_resource(ctx, params):
        return mcp_types.ReadResourceResult(
            contents=[
                mcp_types.TextResourceContents(uri=params.uri, text="late resource")
            ]
        )

    server.add_request_handler(
        "resources/read", mcp_types.ReadResourceRequestParams, late_read_resource
    )

    result = await _call_tool(server, "anything", {"context": "late registration"})
    resource = await _resource_request(
        server,
        "resources/read",
        mcp_types.ReadResourceRequestParams(uri="file:///late.txt"),
    )
    await _flush()

    assert result.content[0].text == "late ok"
    assert resource.contents[0].text == "late resource"
    calls = _events(client, "$mcp_tool_call")
    assert len(calls) == 1
    assert calls[0]["properties"]["$mcp_tool_name"] == "anything"
    reads = _events(client, "$mcp_resource_read")
    assert len(reads) == 1
    assert reads[0]["properties"]["$mcp_resource_name"] == "file:///late.txt"


async def test_initialize_and_session_reuse_across_calls():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    await _call_tool(server, "add", {"a": 1, "b": 1, "context": "first"})
    await _call_tool(server, "add", {"a": 2, "b": 2, "context": "second"})
    await _flush()

    assert len(_events(client, "$mcp_initialize")) == 1
    calls = _events(client, "$mcp_tool_call")
    session_ids = {c["properties"]["$session_id"] for c in calls}
    assert len(session_ids) == 1


async def test_instrument_is_idempotent():
    server = make_server()
    client = FakeClient()
    instrument(server, client)
    wrapped = server.get_request_handler("tools/call").handler
    instrument(server, client)
    assert server.get_request_handler("tools/call").handler is wrapped


async def test_report_missing_appends_virtual_tool():
    server = make_server()
    client = FakeClient()
    instrument(
        server,
        client,
        MCPAnalyticsOptions(report_missing=True, enable_conversation_id=True),
    )

    result = await _list_tools(server)
    names = [t.name for t in result.tools]
    assert "get_more_tools" in names
    virtual = next(t for t in result.tools if t.name == "get_more_tools")
    assert "conversation_id" in virtual.input_schema["properties"]

    call_result = await _call_tool(
        server, "get_more_tools", {"context": "need an email tool"}
    )
    await _flush()

    assert call_result.is_error is False
    missing = _events(client, "$mcp_missing_capability")
    assert missing and missing[0]["properties"]["$mcp_intent"] == "need an email tool"
    handle = missing[0]["properties"]["$mcp_conversation_id"]
    assert handle in call_result.content[1].text


async def test_collect_feedback_appends_virtual_tool():
    server = make_server()
    client = FakeClient()
    instrument(
        server,
        client,
        MCPAnalyticsOptions(collect_feedback=True, enable_conversation_id=True),
    )

    result = await _list_tools(server)
    assert "send_feedback" in [t.name for t in result.tools]
    virtual = next(t for t in result.tools if t.name == "send_feedback")
    assert "conversation_id" in virtual.input_schema["properties"]

    call_result = await _call_tool(
        server,
        "send_feedback",
        {"feedback_type": "issue", "summary": "add rejects floats."},
    )
    await _flush()

    assert call_result.is_error is False
    feedback = _events(client, "$mcp_feedback")
    assert len(feedback) == 1
    assert feedback[0]["properties"]["$mcp_feedback_type"] == "issue"
    assert "$mcp_parameters" not in feedback[0]["properties"]
    handle = feedback[0]["properties"]["$mcp_conversation_id"]
    assert handle in call_result.content[1].text
    assert _events(client, "$mcp_tool_call") == []


async def test_collect_feedback_collision_fails_open_after_listing():
    async def on_call_tool(ctx, params):
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="real tool ran")]
        )

    async def on_list_tools(ctx, params):
        return mcp_types.ListToolsResult(
            tools=[
                mcp_types.Tool(
                    name="send_feedback",
                    description="A real application tool",
                    input_schema={
                        "type": "object",
                        "properties": {"note": {"type": "string"}},
                    },
                )
            ]
        )

    server = Server(
        "low-v2-feedback-collision",
        on_call_tool=on_call_tool,
        on_list_tools=on_list_tools,
    )
    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(collect_feedback=True))

    result = await _list_tools(server)
    assert [t.name for t in result.tools].count("send_feedback") == 1

    out = await _call_tool(server, "send_feedback", {"note": "hi"})
    await _flush()

    assert out.content[0].text == "real tool ran"
    assert _events(client, "$mcp_feedback") == []
    assert _events(client, "$mcp_tool_call")


async def test_callbacks_can_read_headers_through_the_helper():
    """The same `identify` body must work on both SDK majors: `extra["ctx"]` is
    the SDK's own context and `get_request_headers` normalises the read."""
    from posthog.mcp import get_request_headers

    server = make_server()
    client = FakeClient()
    seen = {}

    def identify(request, extra):
        seen["headers"] = get_request_headers(extra)
        return None

    instrument(server, client, MCPAnalyticsOptions(identify=identify))

    ctx = fake_ctx(headers={"Authorization": "Bearer t0ken", "User-Agent": "probe/1"})
    await _call_tool(server, "add", {"a": 1, "b": 1, "context": "header read"}, ctx=ctx)
    await _flush()

    assert seen["headers"] == {"authorization": "Bearer t0ken", "user-agent": "probe/1"}


# --- virtual tools on a paginated listing ------------------------------------

_REAL_GET_MORE_TOOLS_V2 = mcp_types.Tool(
    name="get_more_tools",
    description="A real application tool that owns the name",
    input_schema={"type": "object", "properties": {"context": {"type": "string"}}},
)
_ECHO_TOOL_V2 = mcp_types.Tool(
    name="echo",
    description="Echo",
    input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
)


def make_paged_server_v2(pages):
    """A raw v2 low-level server serving ``pages`` one page per request, chained
    by ``next_cursor``. The tool handler answers ``real tool ran``."""

    async def on_call_tool(ctx, params):
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="real tool ran")]
        )

    async def on_list_tools(ctx, params):
        cursor = getattr(params, "cursor", None)
        index = int(cursor) if cursor else 0
        return mcp_types.ListToolsResult(
            tools=list(pages[index]),
            next_cursor=str(index + 1) if index + 1 < len(pages) else None,
        )

    return Server(
        "test-paged-v2",
        on_call_tool=on_call_tool,
        on_list_tools=on_list_tools,
    )


async def _list_page_v2(server, cursor=None):
    """Request one page. ``cursor=None`` is a first page; any string -- including
    ``""``, a valid opaque cursor -- is a continuation."""
    entry = server.get_request_handler("tools/list")
    params = (
        mcp_types.PaginatedRequestParams(cursor=cursor) if cursor is not None else None
    )
    return await entry.handler(fake_ctx(method="tools/list"), params)


async def test_v2_virtual_tools_appended_to_first_page_only():
    server = make_paged_server_v2([[_ECHO_TOOL_V2], [_ECHO_TOOL_V2]])
    instrument(
        server,
        FakeClient(),
        MCPAnalyticsOptions(report_missing=True, collect_feedback=True),
    )

    first = await _list_page_v2(server)
    second = await _list_page_v2(server, cursor="1")
    assert [t.name for t in first.tools] == ["echo", "get_more_tools", "send_feedback"]
    assert [t.name for t in second.tools] == ["echo"]


async def test_v2_empty_string_cursor_is_a_continuation_page():
    server = make_paged_server_v2([[_ECHO_TOOL_V2]])
    instrument(server, FakeClient(), MCPAnalyticsOptions(report_missing=True))

    page = await _list_page_v2(server, cursor="")
    assert [t.name for t in page.tools] == ["echo"]


async def test_v2_raw_list_probe_blocks_interception_before_any_listing():
    # A raw v2 low-level server has no tool registry, so ownership is settled by
    # asking the host's own tools/list handler.
    server = make_paged_server_v2([[_REAL_GET_MORE_TOOLS_V2]])
    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(report_missing=True))

    result = await _call_tool(server, "get_more_tools", {"context": "need csv"})
    await _flush()

    assert result.content[0].text == "real tool ran"
    assert _events(client, "$mcp_missing_capability") == []


async def test_v2_first_page_collision_lets_the_real_tool_win():
    server = make_paged_server_v2([[_REAL_GET_MORE_TOOLS_V2], [_ECHO_TOOL_V2]])
    client = FakeClient()
    messages = []
    instrument(
        server,
        client,
        MCPAnalyticsOptions(report_missing=True, logger=messages.append),
    )

    first = await _list_page_v2(server)
    second = await _list_page_v2(server, cursor="1")
    listed = [t.name for t in first.tools] + [t.name for t in second.tools]
    assert listed.count("get_more_tools") == 1  # the real tool, never appended
    assert any("Cannot inject PostHog's" in m for m in messages)
    assert any("missing_capability_tool_name" in m for m in messages)


async def test_v2_cached_result_object_does_not_collide_with_itself():
    # v2 returns ListToolsResult directly rather than wrapped in a root model,
    # so it exercises the other branch of the non-mutating append. A host is
    # free to hand back the same object every time; PostHog must not read its
    # own injected tool back out of it as a real one.
    cached = mcp_types.ListToolsResult(tools=[_ECHO_TOOL_V2])

    async def on_call_tool(ctx, params):
        raise ValueError(f"Unknown tool: {params.name}")

    async def on_list_tools(ctx, params):
        return cached

    server = Server(
        "test-cached-v2", on_call_tool=on_call_tool, on_list_tools=on_list_tools
    )
    client = FakeClient()
    messages = []
    instrument(
        server,
        client,
        MCPAnalyticsOptions(report_missing=True, logger=messages.append),
    )

    first = [t.name for t in (await _list_page_v2(server)).tools]
    second = [t.name for t in (await _list_page_v2(server)).tools]
    assert first == ["echo", "get_more_tools"]
    assert second == first
    assert [t.name for t in cached.tools] == ["echo"]  # the host's object is untouched

    result = await _call_tool(server, "get_more_tools", {"context": "need csv"})
    await _flush()
    assert result.content[0].text == get_more_tools_result_text()
    assert _events(client, "$mcp_missing_capability")
    assert not [m for m in messages if "Cannot inject PostHog's" in m]
