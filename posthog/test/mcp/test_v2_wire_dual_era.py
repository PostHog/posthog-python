"""Wire-level dual-era gate for MCP Python SDK v2 (analog of the posthog-js
``harness/dual-era`` matrix, scaled to pytest).

Drives a real ``streamable_http_app`` with raw JSON-RPC over httpx's
ASGITransport — deliberately not the SDK ``Client``, which negotiates the
legacy era and would never exercise 2026-07-28. Two lanes:

* **legacy** (2025-11-25): initialize handshake, then tools/list + tools/call.
* **modern** (2026-07-28): no initialize; every request carries the reserved
  ``_meta`` envelope and the ``MCP-Protocol-Version``/``Mcp-Method``/``Mcp-Name``
  headers.

The stateless topology (``stateless_http=True``, fresh transport per request)
is the one the new spec is built around and the one that broke the JS SDK in
posthog-js#4449 — so it is the only topology tested here.
"""

import json
from contextlib import asynccontextmanager

import httpx
from mcp.server.mcpserver import MCPServer

from posthog.mcp import (
    decode_session_id,
    derive_session_id_from_conversation,
    instrument,
)
from posthog.mcp.types import MCPAnalyticsOptions
from posthog.test.mcp._helpers import (
    FakeClient,
    events_named as _events,
    flush_background as _flush,
)
from posthog.test.mcp._helpers_v2 import (
    LEGACY_PROTOCOL_VERSION,
    MODERN_PROTOCOL_VERSION,
    legacy_headers,
    modern_headers,
    modern_meta,
)


def make_server():
    server = MCPServer("wire-server")

    @server.tool()
    def add(a: int, b: int) -> int:
        return a + b

    @server.tool()
    def boom() -> str:
        raise ValueError("wire explode")

    return server


@asynccontextmanager
async def wire(server):
    """The instrumented server as a live HTTP surface, without binding a port."""
    app = server.streamable_http_app(json_response=True, stateless_http=True)
    async with server.session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8000"
        ) as http:
            yield http


def rpc(method, params, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


async def modern_call(http, name, arguments, request_id=1):
    body = rpc(
        "tools/call",
        {"name": name, "arguments": arguments, "_meta": modern_meta()},
        request_id,
    )
    return await http.post(
        "/mcp", json=body, headers=modern_headers("tools/call", name)
    )


# --- modern era (2026-07-28) ---------------------------------------------------


async def test_modern_tool_call_captured_with_envelope_identity():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    async with wire(server) as http:
        response = await modern_call(
            http, "add", {"a": 2, "b": 3, "context": "adding on the wire"}
        )
    await _flush()

    assert response.status_code == 200
    payload = response.json()
    assert payload["result"]["isError"] is False
    assert payload["result"]["content"][0]["text"] == "5"
    # 2026-07-28 removed protocol sessions: the header must not come back
    assert response.headers.get("mcp-session-id") is None

    calls = _events(client, "$mcp_tool_call")
    assert len(calls) == 1
    props = calls[0]["properties"]
    assert props["$mcp_tool_name"] == "add"
    assert props["$mcp_intent"] == "adding on the wire"
    assert props["$mcp_client_name"] == "wire-client"
    assert props["$mcp_client_version"] == "1.2.3"
    assert props["$mcp_protocol_version"] == MODERN_PROTOCOL_VERSION


async def test_modern_tools_list_advertises_injected_params():
    server = make_server()
    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(enable_conversation_id=True))

    async with wire(server) as http:
        body = rpc("tools/list", {"_meta": modern_meta()})
        response = await http.post(
            "/mcp", json=body, headers=modern_headers("tools/list")
        )
    await _flush()

    assert response.status_code == 200
    tools = {t["name"]: t for t in response.json()["result"]["tools"]}
    assert "context" in tools["add"]["inputSchema"]["properties"]
    assert "conversation_id" in tools["add"]["inputSchema"]["properties"]

    listed = _events(client, "$mcp_tools_list")
    assert listed and set(listed[0]["properties"]["$mcp_listed_tool_names"]) == {
        "add",
        "boom",
    }


async def test_modern_error_is_captured_and_returned():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    async with wire(server) as http:
        response = await modern_call(http, "boom", {"context": "expected to fail"})
    await _flush()

    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True

    calls = _events(client, "$mcp_tool_call")
    assert calls and calls[0]["properties"]["$mcp_is_error"] is True
    assert _events(client, "$exception")


async def test_modern_conversation_anchors_session_across_instances():
    """The cross-pod contract (ADR-0004): two fresh server instances that never
    shared state agree on ``$session_id`` through the agent-echoed handle alone.
    On 2026-07-28 there is no session header, so this is the only correlation."""
    client = FakeClient()
    options = MCPAnalyticsOptions(enable_conversation_id=True)

    # Pod A: the agent sends no handle, so the SDK mints one and prompts back.
    server_a = make_server()
    instrument(server_a, client, options)
    async with wire(server_a) as http:
        response = await modern_call(
            http, "add", {"a": 1, "b": 1, "context": "first call"}
        )
    await _flush()

    # The handle rides back as plain data, not an instruction (a server sentence
    # in a tool result is prompt-injection-shaped and clients may strip it).
    content = response.json()["result"]["content"]
    minted = next(
        json.loads(block["text"])["conversation_id"]
        for block in content
        if block.get("text", "").startswith("{")
        and "conversation_id" in block.get("text", "")
    )

    # Pod B: a different process; the agent echoes the handle.
    server_b = make_server()
    instrument(server_b, client, options)
    async with wire(server_b) as http:
        await modern_call(
            http,
            "add",
            {"a": 2, "b": 2, "context": "second call", "conversation_id": minted},
        )
    await _flush()

    calls = _events(client, "$mcp_tool_call")
    assert len(calls) == 2
    expected = derive_session_id_from_conversation(minted)
    # Pod A anchors on the handle it minted (delivery was confirmed before the
    # session was resolved), and pod B derives the same session from the echo
    # *without ever having met pod A*. That agreement, across two processes
    # sharing no state, is the cross-pod contract.
    assert [c["properties"]["$session_id"] for c in calls] == [expected, expected]
    assert [c["properties"]["$mcp_conversation_id"] for c in calls] == [minted, minted]


async def test_modern_result_shape_survives_instrumentation():
    """Alive check: the instrumented result matches the bare server's, minus
    analytics-owned additions."""
    bare = make_server()
    async with wire(bare) as http:
        bare_response = await modern_call(http, "add", {"a": 4, "b": 5})

    instrumented = make_server()
    instrument(instrumented, FakeClient())
    async with wire(instrumented) as http:
        response = await modern_call(http, "add", {"a": 4, "b": 5, "context": "alive"})
    await _flush()

    bare_result = bare_response.json()["result"]
    result = response.json()["result"]
    assert result["content"] == bare_result["content"]
    assert result["isError"] is bare_result["isError"]
    assert result.get("structuredContent") == bare_result.get("structuredContent")


# --- legacy era (2025-11-25) on the v2 SDK -------------------------------------


async def test_legacy_handshake_lane_still_works():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    async with wire(server) as http:
        init = rpc(
            "initialize",
            {
                "protocolVersion": LEGACY_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "legacy-probe", "version": "0.1"},
            },
        )
        response = await http.post("/mcp", json=init, headers=legacy_headers())
        assert response.status_code == 200
        session_header = response.headers.get("mcp-session-id")

        await http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=legacy_headers(),
        )

        headers = legacy_headers()
        if session_header:
            headers["mcp-session-id"] = session_header
        call = rpc(
            "tools/call",
            {"name": "add", "arguments": {"a": 5, "b": 6, "context": "legacy lane"}},
            2,
        )
        response = await http.post("/mcp", json=call, headers=headers)
    await _flush()

    assert response.status_code == 200
    assert json.loads(response.content)["result"]["content"][0]["text"] == "11"

    calls = _events(client, "$mcp_tool_call")
    assert len(calls) == 1
    assert calls[0]["properties"]["$mcp_tool_name"] == "add"
    assert calls[0]["properties"]["$mcp_is_error"] is False


async def test_legacy_stateless_token_survives_across_instances():
    """The multi-pod legacy story on the v2 SDK: instrument() auto-wires the
    self-encoded ``Mcp-Session-Id`` token onto the stateless app, so client
    identity and one ``$session_id`` survive a fresh server instance when the
    client replays the header."""
    client = FakeClient()

    # Pod A mints the token on initialize.
    server_a = make_server()
    instrument(server_a, client)
    async with wire(server_a) as http:
        init = rpc(
            "initialize",
            {
                "protocolVersion": LEGACY_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "legacy-probe", "version": "0.1"},
            },
        )
        response = await http.post("/mcp", json=init, headers=legacy_headers())
    token_header = response.headers.get("mcp-session-id")
    token = decode_session_id(token_header)
    assert token is not None
    assert token.client_name == "legacy-probe"
    assert token.protocol_version == LEGACY_PROTOCOL_VERSION

    # Pod B — a fresh process that never saw the handshake — gets the replay.
    # A compliant legacy client replays both the session header and the
    # negotiated MCP-Protocol-Version on every subsequent request.
    server_b = make_server()
    instrument(server_b, client)
    async with wire(server_b) as http:
        headers = {
            **legacy_headers(),
            "mcp-session-id": token_header,
            "mcp-protocol-version": LEGACY_PROTOCOL_VERSION,
        }
        call = rpc(
            "tools/call",
            {"name": "add", "arguments": {"a": 1, "b": 2, "context": "pod B"}},
            2,
        )
        response = await http.post("/mcp", json=call, headers=headers)
    await _flush()

    assert response.status_code == 200
    calls = _events(client, "$mcp_tool_call")
    assert len(calls) == 1
    props = calls[0]["properties"]
    # identity recovered from the token, not the (nonexistent) handshake
    assert props["$mcp_client_name"] == "legacy-probe"
    assert props["$mcp_protocol_version"] == LEGACY_PROTOCOL_VERSION
    assert props["$session_id"] == token.session_id


async def test_modern_rejects_initialize_but_analytics_stays_out_of_it():
    """The SDK itself refuses initialize on a modern-locked flow — analytics
    must not change that error contract."""
    server = make_server()
    instrument(server, FakeClient())

    async with wire(server) as http:
        # initialize carrying a modern envelope is a protocol error in v2
        body = rpc(
            "initialize",
            {
                "protocolVersion": MODERN_PROTOCOL_VERSION,
                "capabilities": {},
                "_meta": modern_meta(),
            },
        )
        response = await http.post("/mcp", json=body, headers=legacy_headers())

    # whatever the SDK answers (error payload), the app must not 500
    assert response.status_code < 500


async def test_real_request_headers_reach_the_event():
    """Surface attribution over a *real* HTTP request, not a synthetic context.

    The unit tests drive a hand-built headers mapping; this drives Starlette's
    own `Headers` object through the actual transport, which is where the
    feature has to work — a Python server reporting 100% "Other" in the harness
    breakdown is the symptom this closes.
    """
    from posthog.mcp.constants import PostHogMCPAnalyticsProperty as P

    server = make_server()
    client = FakeClient()
    instrument(server, client)

    user_agent = "claude-code/2.1.0 (cli)"
    async with wire(server) as http:
        body = rpc(
            "tools/call",
            {
                "name": "add",
                "arguments": {"a": 1, "b": 2, "context": "real header check"},
                "_meta": modern_meta(),
            },
        )
        headers = {
            **modern_headers("tools/call", "add"),
            "user-agent": user_agent,
            "x-anthropic-client": "cli",
        }
        response = await http.post("/mcp", json=body, headers=headers)
    await _flush()

    assert response.status_code == 200
    props = _events(client, "$mcp_tool_call")[0]["properties"]
    assert props[P.CLIENT_USER_AGENT] == user_agent
    assert props[P.VENDOR_CLIENT] == "cli"
