"""Conversation-anchored sessions (posthog-js ADR-0004) — runs under both MCP majors.

The 2026-07-28 revision removed protocol-level sessions, so the only thing that
can carry a session across stateless pods is the agent-echoed
``conversation_id`` handle. ``$session_id`` is derived from it deterministically
and unsalted so two pods that never met still agree — and the derivation is a
cross-SDK contract: posthog-js's ``deriveSessionIdFromConversation`` and this
package's ``derive_session_id_from_conversation`` must match byte for byte.
"""

import pytest

from posthog.mcp import derive_session_id_from_conversation
from posthog.mcp._conversation_id import resolve_conversation_id
from posthog.mcp.session import resolve_session_id
from posthog.mcp._internal import MCPAnalyticsData
from posthog.mcp.types import MCPAnalyticsOptions
from posthog.test.mcp._helpers import (
    MCP_MAJOR,
    FakeClient,
    events_named as _events,
    flush_background as _flush,
)

# A handle shaped exactly like the ids we mint (lowercase uuidv7).
MINTED_SHAPE_HANDLE = "0198d3a7-1111-7222-8333-444455556666"


# --- the cross-SDK derivation contract ----------------------------------------


def test_derivation_matches_the_typescript_sdk_byte_for_byte():
    # Vectors computed from posthog-js/packages/mcp `deriveSessionIdFromConversation`
    # (src/extensions/ids.ts). If this test fails, the same conversation splits
    # into two sessions depending on which SDK served the call — do NOT update
    # the expectations without changing both SDKs in lockstep.
    assert (
        derive_session_id_from_conversation("conv-123")
        == "ses_19c018eaeb9263330c016d3a3a41474b"
    )
    assert (
        derive_session_id_from_conversation("0198d3a7-1111-7222-8333-444455556666")
        == "ses_57a5f3768678e803a4af9566ca8a661b"
    )
    assert (
        derive_session_id_from_conversation("a")
        == "ses_8601ec8c0eec655f4ec03fd0b1129ba7"
    )


def test_derivation_is_deterministic_and_distinct():
    assert derive_session_id_from_conversation(
        "h1"
    ) == derive_session_id_from_conversation("h1")
    assert derive_session_id_from_conversation(
        "h1"
    ) != derive_session_id_from_conversation("h2")


# --- the minted-shape guard -----------------------------------------------------


def test_echo_of_a_mintable_handle_is_accepted():
    cid, minted = resolve_conversation_id(
        True, {"conversation_id": MINTED_SHAPE_HANDLE}, "t", "get_more_tools"
    )
    assert minted is False
    assert cid == MINTED_SHAPE_HANDLE


def test_uppercased_echo_is_lowercased_before_hashing():
    # Some hosts normalise uuids to uppercase; the hash behind $session_id is
    # case-sensitive, so the echo must be folded back or it lands in a
    # different session than the call that minted it.
    cid, minted = resolve_conversation_id(
        True, {"conversation_id": MINTED_SHAPE_HANDLE.upper()}, "t", "get_more_tools"
    )
    assert minted is False
    assert cid == MINTED_SHAPE_HANDLE


def test_invented_handle_is_not_anchored():
    # Two unrelated users both sending "conv-1" must NOT share a session, so a
    # value we could not have minted is replaced with a fresh handle.
    cid, minted = resolve_conversation_id(
        True, {"conversation_id": "conv-1"}, "t", "get_more_tools"
    )
    assert minted is True
    assert cid != "conv-1"


# --- session resolution ---------------------------------------------------------


async def test_conversation_wins_session_resolution_without_touching_state():
    data = MCPAnalyticsData(
        options=MCPAnalyticsOptions(), sink=None, session_id="ses_memory"
    )
    before = data.session_id

    resolved = await resolve_session_id(
        data, "transport-session", conversation_id=MINTED_SHAPE_HANDLE
    )

    assert resolved == derive_session_id_from_conversation(MINTED_SHAPE_HANDLE)
    # per-request, never sticky: the shared state a concurrent chat reads is untouched
    assert data.session_id == before


async def test_without_conversation_resolution_is_unchanged():
    data = MCPAnalyticsData(
        options=MCPAnalyticsOptions(), sink=None, session_id="ses_memory"
    )
    resolved = await resolve_session_id(data, "transport-session")
    assert resolved != derive_session_id_from_conversation(MINTED_SHAPE_HANDLE)
    assert resolved.startswith("ses_")


# --- end-to-end on the v1 high-level server (anchoring is not era-gated) --------


@pytest.mark.skipif(MCP_MAJOR != 1, reason="v1 FastMCP server")
async def test_v1_calls_sharing_a_handle_land_in_one_session():
    from mcp.server.fastmcp import FastMCP
    from posthog.mcp import instrument

    server = FastMCP("conv-v1")

    @server.tool()
    def echo(msg: str) -> str:
        return msg

    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(enable_conversation_id=True))

    await server._tool_manager.call_tool(
        "echo", {"msg": "a", "conversation_id": MINTED_SHAPE_HANDLE, "context": "first"}
    )
    await server._tool_manager.call_tool(
        "echo",
        {"msg": "b", "conversation_id": MINTED_SHAPE_HANDLE, "context": "second"},
    )
    await _flush()

    calls = _events(client, "$mcp_tool_call")
    expected = derive_session_id_from_conversation(MINTED_SHAPE_HANDLE)
    assert [c["properties"]["$session_id"] for c in calls] == [expected, expected]
    assert [c["properties"]["$mcp_conversation_id"] for c in calls] == [
        MINTED_SHAPE_HANDLE,
        MINTED_SHAPE_HANDLE,
    ]


@pytest.mark.skipif(MCP_MAJOR != 1, reason="v1 FastMCP server")
async def test_v1_minted_then_echoed_reuses_one_session():
    from mcp.server.fastmcp import FastMCP
    from posthog.mcp import instrument

    server = FastMCP("conv-v1-mint")

    @server.tool()
    def echo(msg: str) -> str:
        return msg

    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(enable_conversation_id=True))

    # First call omits the handle: the SDK mints one and prompts the agent for it.
    # convert_result=True is the production shape (FastMCP's call path), giving
    # the (content, structured) tuple the prompt-back can ride.
    await server._tool_manager.call_tool(
        "echo", {"msg": "a", "context": "first"}, convert_result=True
    )
    await _flush()
    first = _events(client, "$mcp_tool_call")[0]["properties"]
    minted = first["$mcp_conversation_id"]
    assert minted

    # The agent echoes it back.
    await server._tool_manager.call_tool(
        "echo", {"msg": "b", "conversation_id": minted, "context": "second"}
    )
    await _flush()

    calls = _events(client, "$mcp_tool_call")
    expected = derive_session_id_from_conversation(minted)
    assert [c["properties"]["$session_id"] for c in calls] == [expected, expected]


@pytest.mark.skipif(MCP_MAJOR != 1, reason="v1 FastMCP server")
async def test_v1_structured_output_tool_gets_the_conversation_handle():
    """Clients that read ``structuredContent`` never render ``content``, so a
    tool declaring an output schema must get the handle mirrored into the
    structured half or the agent can never echo it back (ADR-0004)."""
    from typing import Any

    import mcp.types as mcp_types
    from mcp.server.fastmcp import FastMCP

    from posthog.mcp import instrument
    from posthog.mcp._output_instructions import MCP_INSTRUCTIONS_KEY

    server = FastMCP("structured-v1")

    @server.tool()
    def totals(event: str) -> dict[str, Any]:
        return {"event": event, "total": 7}

    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(enable_conversation_id=True))

    handler = server._mcp_server.request_handlers[mcp_types.ListToolsRequest]
    listed = await handler(mcp_types.ListToolsRequest(method="tools/list"))
    tool = next(t for t in listed.root.tools if t.name == "totals")
    assert MCP_INSTRUCTIONS_KEY in tool.outputSchema["properties"]

    result = await server._tool_manager.call_tool(
        "totals", {"event": "pageview", "context": "structured"}, convert_result=True
    )
    await _flush()

    structured = result[1]
    handle = structured[MCP_INSTRUCTIONS_KEY]["conversation_id"]
    assert handle
    assert structured["total"] == 7
    assert (
        _events(client, "$mcp_tool_call")[0]["properties"]["$mcp_conversation_id"]
        == handle
    )


@pytest.mark.skipif(MCP_MAJOR != 1, reason="v1 FastMCP server")
async def test_v1_feature_off_keeps_transport_sessions():
    from mcp.server.fastmcp import FastMCP
    from posthog.mcp import instrument

    server = FastMCP("conv-v1-off")

    @server.tool()
    def echo(msg: str) -> str:
        return msg

    client = FakeClient()
    instrument(server, client)  # enable_conversation_id defaults off

    await server._tool_manager.call_tool("echo", {"msg": "a", "context": "x"})
    await _flush()

    props = _events(client, "$mcp_tool_call")[0]["properties"]
    assert "$mcp_conversation_id" not in props
    assert props["$session_id"] != derive_session_id_from_conversation(
        MINTED_SHAPE_HANDLE
    )
