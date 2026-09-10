"""Resource tracking through the real high-level server adapters."""

import json

import mcp.types as mcp_types
import pytest

from posthog.mcp import instrument
from posthog.test.mcp._helpers import (
    MCP_MAJOR,
    FakeClient,
    events_named,
    flush_background,
    listed_uris,
)


@pytest.fixture(params=["official", "fastmcp"])
def server(request):
    if request.param == "fastmcp":
        if MCP_MAJOR >= 2:
            pytest.skip("jlowin FastMCP requires MCP SDK v1")
        pytest.importorskip("fastmcp")
        from fastmcp import FastMCP as Server
    elif MCP_MAJOR < 2:
        from mcp.server.fastmcp import FastMCP as Server
    else:
        from mcp.server.mcpserver import MCPServer as Server

    return Server("resource-test")


_V1_REQUEST_TYPES = {
    "resources/list": mcp_types.ListResourcesRequest,
    "resources/templates/list": mcp_types.ListResourceTemplatesRequest,
    "resources/read": mcp_types.ReadResourceRequest,
}


async def dispatch(server, method, params=None):
    """Send one request through the server's own handler registry, whichever MCP
    SDK major is installed."""
    if MCP_MAJOR < 2:
        request = _V1_REQUEST_TYPES[method](params=params)
        handler = server._mcp_server.request_handlers[type(request)]
        return (await handler(request)).root

    from posthog.test.mcp._helpers_v2 import fake_ctx

    entry = server._lowlevel_server.get_request_handler(method)
    return await entry.handler(fake_ctx(method=method), params)


@pytest.mark.parametrize("register_late", [False, True])
@pytest.mark.parametrize("resource_error", [False, True])
async def test_highlevel_resource_tracking(
    server, register_late: bool, resource_error: bool
) -> None:
    client = FakeClient()
    body = " ".join(["Private", "document", "contents"])
    if register_late:
        instrument(server, client)

    @server.resource("file:///guide.md")
    async def guide() -> str:
        if resource_error:
            raise ValueError("resource unavailable")
        return body

    instrument(server, client)

    listing = await dispatch(server, "resources/list")
    assert str(listing.resources[0].uri) == "file:///guide.md"
    read = dispatch(
        server,
        "resources/read",
        mcp_types.ReadResourceRequestParams(uri="file:///guide.md"),
    )
    if resource_error:
        message = "resource unavailable" if MCP_MAJOR < 2 else "Error reading resource"
        with pytest.raises(Exception, match=message):
            await read
    else:
        result = await read
        assert result.contents[0].text == body
        assert str(result.contents[0].uri) == "file:///guide.md"
    await flush_background()

    lists = events_named(client, "$mcp_resources_list")
    assert len(lists) == 1
    list_props = lists[0]["properties"]
    assert list_props["$mcp_parameters"]["request"]["method"] == "resources/list"
    assert listed_uris(list_props["$mcp_response"]) == ["file:///guide.md"]
    assert list_props["$mcp_is_error"] is False
    assert list_props["$mcp_duration_ms"] >= 0
    assert "$mcp_resource_name" not in list_props

    reads = events_named(client, "$mcp_resource_read")
    assert len(reads) == 1
    props = reads[0]["properties"]
    assert props["$mcp_resource_name"] == "file:///guide.md"
    assert props["$mcp_is_error"] is resource_error
    assert props["$mcp_duration_ms"] >= 0
    assert "$mcp_response" not in props
    assert len(events_named(client, "$exception")) == int(resource_error)
    assert body not in json.dumps(client.events)


async def test_highlevel_resource_templates_listing_is_captured(server) -> None:
    client = FakeClient()

    @server.resource("users://{user_id}/profile")
    async def profile(user_id: str) -> str:
        return f"profile for {user_id}"

    instrument(server, client)

    await dispatch(server, "resources/templates/list")
    await flush_background()

    lists = events_named(client, "$mcp_resources_list")
    assert len(lists) == 1
    props = lists[0]["properties"]
    # Same event as resources/list; the captured request method is what tells a
    # template listing apart.
    assert props["$mcp_parameters"]["request"]["method"] == "resources/templates/list"
    assert listed_uris(props["$mcp_response"]) == ["users://{user_id}/profile"]
    assert props["$mcp_is_error"] is False


async def test_failed_read_reports_the_handler_failure(server) -> None:
    """The SDK wraps a failing read in its own error before it reaches the caller.
    The captured failure detail follows that chain to what the handler actually
    raised, while the caller still receives the SDK's wrapper unchanged."""
    client = FakeClient()

    @server.resource("file:///guide.md")
    async def guide() -> str:
        raise TimeoutError("storage backend timed out")

    instrument(server, client)

    with pytest.raises(Exception, match="Error reading resource"):
        await dispatch(
            server,
            "resources/read",
            mcp_types.ReadResourceRequestParams(uri="file:///guide.md"),
        )
    await flush_background()

    props = events_named(client, "$mcp_resource_read")[0]["properties"]
    assert props["$mcp_is_error"] is True
    assert "storage backend timed out" in props["$mcp_error_message"]
    # v2 masks the handler's message out of its wrapper, so the scalars step past
    # it. v1's wrapper keeps that message, and is reported as it always was.
    expected_type = "TimeoutError" if MCP_MAJOR >= 2 else "ResourceError"
    assert props["$mcp_error_type"] == expected_type
