"""Client identity on mcp>=2, which renamed the SDK seams this SDK hooks.

Skipped on mcp 1.x, where `mcp.server.mcpserver` doesn't exist. Run with an
`mcp>=2` install to exercise it.
"""

import pytest

pytest.importorskip("mcp.server.mcpserver")

import mcp  # noqa: E402
import mcp.types as mcp_types  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402

from posthog.mcp import instrument  # noqa: E402
from posthog.test.mcp._helpers import (  # noqa: E402
    FakeClient,
    events_named as _events,
    flush_background as _flush,
)

CLIENT = mcp_types.Implementation(name="codex", version="1.2.3")


def make_server():
    server = MCPServer("test-server")

    @server.tool()
    def add(a: int, b: int) -> int:
        return a + b

    @server.tool()
    def boom() -> str:
        raise ValueError("explode")

    return server


async def test_captures_client_identity_on_mcp_v2():
    """mcp>=2 renamed `client_params.clientInfo` to `client_info` and puts the
    negotiated version on the request context, so identity must come from there."""
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    async with mcp.Client(server, client_info=CLIENT) as c:
        result = await c.call_tool("add", {"a": 1, "b": 2})
    assert not result.is_error
    await _flush()

    calls = _events(client, "$mcp_tool_call")
    assert calls
    props = calls[0]["properties"]
    assert props["$mcp_tool_name"] == "add"
    assert props["$mcp_client_name"] == "codex"
    assert props["$mcp_client_version"] == "1.2.3"
    assert props["$mcp_protocol_version"] == "2026-07-28"
    assert props["$mcp_is_error"] is False

    init = _events(client, "$mcp_initialize")
    assert init and init[0]["properties"]["$mcp_client_name"] == "codex"


async def test_flags_tool_errors_on_mcp_v2():
    """`CallToolResult.isError` is spelled `is_error` on mcp>=2; without handling
    both, every v2 tool error is recorded as a success."""
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    async with mcp.Client(server, client_info=CLIENT) as c:
        await c.call_tool("boom", {})
    await _flush()

    call = next(
        e
        for e in _events(client, "$mcp_tool_call")
        if e["properties"]["$mcp_tool_name"] == "boom"
    )
    assert call["properties"]["$mcp_is_error"] is True
    assert _events(client, "$exception")
