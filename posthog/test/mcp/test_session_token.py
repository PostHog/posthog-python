"""Tests for the self-encoded session token (stateless / multi-pod fix): the
codec, its use in session resolution, and the ASGI minting middleware."""

from __future__ import annotations

import json

import pytest

from posthog.mcp._internal import MCPAnalyticsData
from posthog.mcp.asgi import (
    PostHogMcpStatelessSessionMiddleware,
    get_mcp_session,
)
from posthog.mcp.session import new_session_id, resolve_session_id
from posthog.mcp.session_token import (
    MCP_SESSION_HEADER,
    SessionTokenPayload,
    decode_session_id,
    encode_session_id,
    read_mcp_session_header,
)
from posthog.mcp.types import MCPAnalyticsOptions


# --- codec -------------------------------------------------------------------


def test_encode_decode_round_trip():
    token = encode_session_id(
        SessionTokenPayload(
            session_id="ses_abc",
            client_name="Claude Code",
            client_version="1.2.3",
            protocol_version="2025-06-18",
        )
    )
    payload = decode_session_id(token)
    assert payload is not None
    assert payload.session_id == "ses_abc"
    assert payload.client_name == "Claude Code"
    assert payload.client_version == "1.2.3"
    assert payload.protocol_version == "2025-06-18"


def test_encode_decode_survives_non_ascii_client_name():
    token = encode_session_id(
        SessionTokenPayload(session_id="ses_1", client_name="クロード🤖")
    )
    payload = decode_session_id(token)
    assert payload is not None and payload.client_name == "クロード🤖"


def test_token_matches_mcp_visible_ascii_session_pattern():
    # The MCP SDK validates a session id header against ^[\x21-\x7E]+$; a base64url
    # token (no padding) must pass so the transport accepts it as the header.
    token = encode_session_id(
        SessionTokenPayload(session_id="ses_abc", client_name="x", client_version="y")
    )
    assert all(0x21 <= ord(ch) <= 0x7E for ch in token)


def test_encode_requires_session_id():
    with pytest.raises(ValueError):
        encode_session_id(SessionTokenPayload(session_id=""))


def test_decode_rejects_non_tokens_without_raising():
    # Transport UUID, JWT-ish (dots), empty, wrong types, and garbage.
    assert decode_session_id("550e8400-e29b-41d4-a716-446655440000") is None
    assert decode_session_id("aaa.bbb.ccc") is None
    assert decode_session_id("") is None
    assert decode_session_id(None) is None
    assert decode_session_id(12345) is None  # type: ignore[arg-type]
    assert decode_session_id("!!!not base64!!!") is None


def test_decode_rejects_oversized_token():
    assert decode_session_id("A" * 5000) is None


def test_decode_rejects_payload_without_sid():
    import base64

    raw = (
        base64.urlsafe_b64encode(json.dumps({"cn": "x"}).encode()).decode().rstrip("=")
    )
    assert decode_session_id(raw) is None


def test_encode_truncates_long_client_fields():
    payload = decode_session_id(
        encode_session_id(
            SessionTokenPayload(session_id="ses_1", client_name="c" * 500)
        )
    )
    assert payload is not None and len(payload.client_name or "") == 200


def test_read_mcp_session_header_case_insensitive_list_and_trim():
    assert read_mcp_session_header({"Mcp-Session-Id": "  tok  "}) == "tok"
    assert read_mcp_session_header({MCP_SESSION_HEADER: ["a", "b"]}) == "a"
    assert read_mcp_session_header({"other": "x"}) is None
    assert read_mcp_session_header({MCP_SESSION_HEADER: "   "}) is None
    assert read_mcp_session_header(None) is None


# --- session resolution ------------------------------------------------------


def _data() -> MCPAnalyticsData:
    data = MCPAnalyticsData(options=MCPAnalyticsOptions())
    data.session_id = new_session_id()
    return data


async def test_resolve_session_id_uses_token_verbatim():
    data = _data()
    token = decode_session_id(
        encode_session_id(
            SessionTokenPayload(session_id="ses_tok", client_name="Cursor")
        )
    )
    sid = await resolve_session_id(data, "ignored-raw", token=token)
    # Used verbatim -- NOT re-hashed through derive_session_id_from_mcp_session.
    assert sid == "ses_tok"
    assert data.session_source == "token"
    assert data.token_client_name == "Cursor"


async def test_token_session_does_not_fragment_or_roll_over():
    from datetime import datetime, timedelta, timezone

    data = _data()
    token = decode_session_id(
        encode_session_id(SessionTokenPayload(session_id="ses_tok"))
    )
    first = await resolve_session_id(data, None, token=token)
    # A later request without the header keeps the id...
    assert await resolve_session_id(data, None) == first
    # ...and it never rolls over on inactivity (only generated sessions do).
    data.last_activity = datetime.now(timezone.utc) - timedelta(minutes=31)
    assert await resolve_session_id(data, None) == first


async def test_multi_pod_two_instances_resolve_same_session_and_harness():
    """The regression this fixes: independent pods (independent per-server state)
    resolve the same replayed token to the same session id + harness."""
    token_str = encode_session_id(
        SessionTokenPayload(session_id="ses_shared", client_name="Claude Code")
    )
    results = []
    for _ in range(2):  # two "pods"
        data = _data()
        token = decode_session_id(token_str)
        sid = await resolve_session_id(data, token_str, token=token)
        results.append((sid, data.session_source, data.token_client_name))
    assert results[0] == results[1] == ("ses_shared", "token", "Claude Code")


# --- ASGI minting middleware -------------------------------------------------


def _init_body(client_name="Claude Code", client_version="1.0.0", pv="2025-06-18"):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": pv,
                "clientInfo": {"name": client_name, "version": client_version},
            },
        }
    ).encode()


async def _run(app, scope, body: bytes):
    """Drive an ASGI app once, returning the response-start headers as a dict."""
    sent = {}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            sent["headers"] = {
                k.decode().lower(): v.decode() for k, v in message.get("headers", [])
            }

    await app(scope, receive, send)
    return sent


def _scope(method="POST", headers=None):
    raw = [(k.encode(), v.encode()) for k, v in (headers or {}).items()]
    return {"type": "http", "method": method, "headers": raw}


async def test_middleware_mints_session_header_on_initialize():
    captured = {}

    async def app(scope, receive, send):
        captured["session"] = get_mcp_session(scope)
        # Read the body downstream to prove the middleware replayed it intact.
        captured["body"] = (await receive())["body"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = PostHogMcpStatelessSessionMiddleware(app)
    scope = _scope()
    body = _init_body()
    sent = await _run(mw, scope, body)

    token = sent["headers"].get(MCP_SESSION_HEADER)
    assert token is not None
    payload = decode_session_id(token)
    assert payload is not None
    assert payload.client_name == "Claude Code"
    assert payload.client_version == "1.0.0"
    assert payload.protocol_version == "2025-06-18"
    # The app saw the recovered session and the untouched body.
    assert captured["session"].session_id == payload.session_id
    assert captured["body"] == body


async def test_middleware_does_not_clobber_existing_response_header():
    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(MCP_SESSION_HEADER.encode(), b"existing-uuid")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    mw = PostHogMcpStatelessSessionMiddleware(app)
    sent = await _run(mw, _scope(), _init_body())
    assert sent["headers"][MCP_SESSION_HEADER] == "existing-uuid"


async def test_middleware_skips_when_client_replays_session_id():
    """A request already carrying a session id (ours or a stateful UUID) is left
    alone; the replayed token is still decoded and exposed to the app."""
    token_str = encode_session_id(
        SessionTokenPayload(session_id="ses_replayed", client_name="Cursor")
    )
    seen = {}

    async def app(scope, receive, send):
        seen["session"] = get_mcp_session(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = PostHogMcpStatelessSessionMiddleware(app)
    scope = _scope(headers={MCP_SESSION_HEADER: token_str})
    sent = await _run(mw, scope, _init_body())
    # No mint (client already has a session id) ...
    assert MCP_SESSION_HEADER not in sent["headers"]
    # ... but the replayed token is decoded for the app.
    assert seen["session"].session_id == "ses_replayed"
    assert seen["session"].client_name == "Cursor"


async def test_middleware_passes_through_non_initialize_post():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = PostHogMcpStatelessSessionMiddleware(app)
    body = json.dumps({"jsonrpc": "2.0", "method": "tools/list", "id": 2}).encode()
    sent = await _run(mw, _scope(), body)
    assert MCP_SESSION_HEADER not in sent["headers"]


async def test_middleware_replays_body_split_across_chunks_and_over_sniff_cap():
    """The buffered body must be replayed byte-for-byte even when it arrives in
    multiple ASGI messages and exceeds the sniff cap (a large POST is never our
    initialize handshake, so we skip minting but must not corrupt the body)."""
    from posthog.mcp import asgi as asgi_mod

    big = b'{"jsonrpc":"2.0","method":"tools/call","params":{"blob":"'
    big += b"x" * (asgi_mod._MAX_SNIFF_BODY + 10)
    big += b'"}}'
    parts = [big[i : i + 4096] for i in range(0, len(big), 4096)]

    received = {}

    async def app(scope, receive, send):
        buf = b""
        while True:
            msg = await receive()
            buf += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        received["body"] = buf
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def receive():
        if parts:
            chunk = parts.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(parts)}
        return {"type": "http.request", "body": b"", "more_body": False}

    sent = {}

    async def send(message):
        if message["type"] == "http.response.start":
            sent["headers"] = {
                k.decode().lower(): v.decode() for k, v in message.get("headers", [])
            }

    mw = PostHogMcpStatelessSessionMiddleware(app)
    await mw(_scope(), receive, send)

    assert received["body"] == big  # byte-faithful, not truncated at the cap
    assert MCP_SESSION_HEADER not in sent["headers"]  # too big to sniff -> no mint


# --- end-to-end against a real stateless FastMCP transport -------------------


def test_middleware_end_to_end_with_stateless_fastmcp():
    """Cross-version sanity: mount a real stateless FastMCP streamable-HTTP app,
    add the middleware, and confirm (1) the `initialize` response carries a
    decodable minted token with the client's harness, and (2) a follow-up request
    on a fresh stateless transport that replays the token is accepted (not
    rejected) -- i.e. the same session survives across pods."""
    pytest.importorskip("starlette.testclient")
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.testclient import TestClient

    srv = FastMCP(
        "posthog-token-test",
        stateless_http=True,
        json_response=True,
        # TestClient sends Host: testserver; allow it past DNS-rebinding protection.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )

    @srv.tool()
    def add(a: int, b: int) -> int:
        return a + b

    app = srv.streamable_http_app()
    app.add_middleware(PostHogMcpStatelessSessionMiddleware)

    hdrs = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    def rpc(method, params=None, id=1, extra=None):
        body = {"jsonrpc": "2.0", "id": id, "method": method}
        if params is not None:
            body["params"] = params
        return {**hdrs, **(extra or {})}, json.dumps(body)

    with TestClient(app) as client:
        h, b = rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "Claude Code", "version": "9.9.9"},
            },
        )
        resp = client.post("/mcp", headers=h, content=b)
        assert resp.status_code == 200, resp.text

        token = resp.headers.get(MCP_SESSION_HEADER)
        payload = decode_session_id(token)
        assert payload is not None, "initialize response did not carry our token"
        assert payload.client_name == "Claude Code"
        assert payload.client_version == "9.9.9"
        assert payload.protocol_version == "2025-06-18"

        # A different pod (fresh stateless transport) replays the token: accepted.
        h2, b2 = rpc(
            "tools/list",
            {},
            id=2,
            extra={MCP_SESSION_HEADER: token, "mcp-protocol-version": "2025-06-18"},
        )
        resp2 = client.post("/mcp", headers=h2, content=b2)
        assert resp2.status_code == 200, resp2.text
