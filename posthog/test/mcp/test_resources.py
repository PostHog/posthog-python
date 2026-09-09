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
)


@pytest.fixture(params=["official", "fastmcp"])
def server(request):
    if request.param == "fastmcp":
        if MCP_MAJOR >= 2:
            pytest.skip("jlowin FastMCP requires MCP SDK v1")
        from fastmcp import FastMCP as Server
    elif MCP_MAJOR < 2:
        from mcp.server.fastmcp import FastMCP as Server
    else:
        from mcp.server.mcpserver import MCPServer as Server

    return Server("resource-test")


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

    async def dispatch(method, params=None):
        if MCP_MAJOR < 2:
            request = (
                mcp_types.ListResourcesRequest()
                if method == "resources/list"
                else mcp_types.ReadResourceRequest(params=params)
            )
            handler = server._mcp_server.request_handlers[type(request)]
            return (await handler(request)).root

        from posthog.test.mcp._helpers_v2 import fake_ctx

        entry = server._lowlevel_server.get_request_handler(method)
        return await entry.handler(fake_ctx(method=method), params)

    listing = await dispatch("resources/list")
    assert str(listing.resources[0].uri) == "file:///guide.md"
    read = dispatch(
        "resources/read", mcp_types.ReadResourceRequestParams(uri="file:///guide.md")
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

    assert len(events_named(client, "$mcp_resources_list")) == 1
    reads = events_named(client, "$mcp_resource_read")
    assert len(reads) == 1
    props = reads[0]["properties"]
    assert props["$mcp_resource_name"] == "file:///guide.md"
    assert props["$mcp_is_error"] is resource_error
    assert props["$mcp_duration_ms"] >= 0
    assert "$mcp_response" not in props
    assert len(events_named(client, "$exception")) == int(resource_error)
    assert body not in json.dumps(client.events)
