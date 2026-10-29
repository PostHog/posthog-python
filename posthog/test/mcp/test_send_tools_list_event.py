"""``send_tools_list_event=False`` sends no ``$mcp_tools_list``, and nothing else changes.

Reuses the real-HTTP fleet from ``test_initialize_handshake.py``, so it runs for
every server shape on both SDK majors.
"""

from __future__ import annotations

import pytest

from posthog.mcp import MCPAnalyticsOptions, instrument
from posthog.test.mcp._helpers import (
    MCP_MAJOR,
    FakeClient,
    events_named as _events,
    flush_background as _flush,
)
from posthog.test.mcp.test_initialize_handshake import (
    KINDS,
    call_add,
    fleet,
    handshake,
    list_tools,
)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("capture", [True, False])
async def test_send_tools_list_event_controls_only_the_listing_event(kind, capture):
    client = FakeClient()
    options = MCPAnalyticsOptions(
        enable_conversation_id=False, send_tools_list_event=capture
    )
    async with fleet(client, kind=kind, options=options) as lb:
        headers, _ = await handshake(lb)
        listing = (await list_tools(lb, headers)).json()["result"]
        await call_add(lb, headers)
    await _flush()

    assert len(_events(client, "$mcp_tools_list")) == (1 if capture else 0)
    # The listing itself is unchanged: the SDK still injects `context`.
    (tool,) = [t for t in listing["tools"] if t["name"] == "add"]
    assert "context" in tool["inputSchema"]["properties"]
    (call,) = _events(client, "$mcp_tool_call")
    assert call["properties"]["$mcp_intent"] == "adding"
    assert len(_events(client, "$mcp_initialize")) == 1


@pytest.mark.skipif(MCP_MAJOR >= 2, reason="the request_handlers registry is mcp 1.x")
async def test_send_tools_list_event_off_also_drops_a_failed_listing():
    import mcp.types as mcp_types
    from mcp.server.lowlevel import Server

    server = Server("failing-listing")

    @server.list_tools()
    async def list_tools_handler():
        raise RuntimeError("listing failed")

    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(send_tools_list_event=False))
    handler = server.request_handlers[mcp_types.ListToolsRequest]
    with pytest.raises(RuntimeError):
        await handler(mcp_types.ListToolsRequest(method="tools/list"))
    await _flush()

    assert _events(client, "$mcp_tools_list") == []
    assert _events(client, "$exception") == []
