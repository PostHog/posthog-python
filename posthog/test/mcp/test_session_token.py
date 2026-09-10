"""Tests for the self-encoded session token (stateless / multi-pod fix): the
codec, its use in session resolution, and the ASGI minting middleware."""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from posthog.mcp._internal import MCPAnalyticsData
from posthog.mcp.asgi import (
    PostHogMcpStatelessSessionMiddleware,
    _app_was_already_built,
    get_mcp_session,
)
from posthog.test.mcp._helpers import MCP_MAJOR
from posthog.mcp import logger as logger_module
from posthog.mcp._instrumentation import prepare_request
from posthog.mcp.logger import set_logger
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


def test_cross_sdk_wire_format_is_frozen():
    """The token must decode in both this SDK and the TypeScript one (same
    base64url of compact JSON with keys sid/cn/cv/pv, in that order). This pins
    the exact wire string for a known payload, so a key rename or encoding change
    on either side fails CI instead of silently breaking cross-SDK reads.

    The TS SDK produces this same string for the same input (`JSON.stringify` is
    compact and preserves insertion order, matching Python's compact `json.dumps`).
    """
    payload = SessionTokenPayload(
        session_id="ses_fixture",
        client_name="Claude Code",
        client_version="1.2.3",
        protocol_version="2025-06-18",
    )
    frozen = "eyJzaWQiOiJzZXNfZml4dHVyZSIsImNuIjoiQ2xhdWRlIENvZGUiLCJjdiI6IjEuMi4zIiwicHYiOiIyMDI1LTA2LTE4In0"

    # Encode side: our output is byte-identical to the shared wire format.
    assert encode_session_id(payload) == frozen

    # Decode side: a token minted elsewhere (e.g. the TS SDK) reads back exactly.
    decoded = decode_session_id(frozen)
    assert decoded is not None
    assert decoded.session_id == "ses_fixture"
    assert decoded.client_name == "Claude Code"
    assert decoded.client_version == "1.2.3"
    assert decoded.protocol_version == "2025-06-18"


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


async def test_token_session_used_every_time_and_never_rolls_over():
    from datetime import datetime, timedelta, timezone

    data = _data()
    token = decode_session_id(
        encode_session_id(SessionTokenPayload(session_id="ses_tok"))
    )
    first = await resolve_session_id(data, None, token=token)
    # Replayed on every request (what a compliant client does) -> same id, and it
    # never rolls over on inactivity (unlike a generated session).
    data.last_activity = datetime.now(timezone.utc) - timedelta(minutes=31)
    assert await resolve_session_id(data, None, token=token) == first


async def test_token_session_not_reused_for_tokenless_request():
    """`data` is shared across clients on one server, so a request that doesn't
    replay the token must NOT inherit the previous client's token session."""
    data = _data()
    token = decode_session_id(
        encode_session_id(SessionTokenPayload(session_id="ses_a"))
    )
    a = await resolve_session_id(data, None, token=token)
    assert a == "ses_a"
    # A different, tokenless client hits the same server -> fresh session, not ses_a.
    b = await resolve_session_id(data, None)
    assert b != "ses_a" and data.session_source == "generated"


async def test_multi_pod_two_instances_resolve_same_session_and_harness():
    """The regression this fixes: independent pods (independent per-server state)
    resolve the same replayed token to the same session id, and the harness is
    recovered from the token by the adapter helper."""
    from posthog.mcp._instrumentation import resolve_session_and_client

    token_str = encode_session_id(
        SessionTokenPayload(
            session_id="ses_shared",
            client_name="Claude Code",
            protocol_version="2025-06-18",
        )
    )
    results = []
    for _ in range(2):  # two "pods"
        data = _data()
        token = decode_session_id(token_str)
        sid = await resolve_session_id(data, token_str, token=token)
        # Harness + protocol version come back per request in the adapters
        # (not from shared state).
        _tok, name, _ver, protocol = resolve_session_and_client(token_str, None, None)
        results.append((sid, data.session_source, name, protocol))
    assert (
        results[0] == results[1] == ("ses_shared", "token", "Claude Code", "2025-06-18")
    )


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


async def test_middleware_survives_adversarial_nested_json():
    """A deeply-nested JSON body makes json.loads raise RecursionError (a
    RuntimeError, not ValueError/TypeError). The fail-safe must swallow it and pass
    the request through untouched -- never surface a 500 from the analytics layer."""
    reached = {"app": False}

    async def app(scope, receive, send):
        reached["app"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = PostHogMcpStatelessSessionMiddleware(app)
    body = b"[" * 20000 + b"0" + b"]" * 20000  # ~40KB, well under the sniff cap
    sent = await _run(mw, _scope(), body)  # must not raise
    assert reached["app"] is True
    assert MCP_SESSION_HEADER not in sent["headers"]


def test_autowire_does_not_double_add_middleware():
    """fastmcp 2.x aliases (streamable_http_app -> http_app) could add the
    middleware twice to one app; the factory guards against that."""
    from starlette.applications import Starlette

    from posthog.mcp.asgi import _wrap_app_factory

    app = Starlette()
    # Simulate a factory that already applied the middleware (the aliased case).
    app.add_middleware(PostHogMcpStatelessSessionMiddleware)

    wrapped = _wrap_app_factory(lambda: app)()

    count = sum(
        1
        for m in wrapped.user_middleware
        if getattr(m, "cls", None) is PostHogMcpStatelessSessionMiddleware
    )
    assert count == 1


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
    pytest.importorskip("mcp.server.fastmcp")  # v1-only server; skipped under mcp>=2
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


def test_instrument_autowires_stateless_mint_no_manual_middleware():
    """instrument() alone (no app.add_middleware) makes the FastMCP streamable-HTTP
    app mint the session token -- the zero-config path. mcp.run() uses the same
    factory internally, so it's covered too."""
    pytest.importorskip("starlette.testclient")
    pytest.importorskip("mcp.server.fastmcp")  # v1-only server; skipped under mcp>=2
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.testclient import TestClient

    from posthog.mcp import instrument

    srv = FastMCP(
        "posthog-autowire-test",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )

    @srv.tool()
    def ping() -> str:
        return "pong"

    # No app.add_middleware() anywhere -- instrument() wires the mint itself.
    instrument(srv, _Sink())
    app = srv.streamable_http_app()

    with TestClient(app) as client:
        resp = client.post(
            "/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "Cursor", "version": "0.42"},
                    },
                }
            ),
        )
        assert resp.status_code == 200, resp.text
        payload = decode_session_id(resp.headers.get(MCP_SESSION_HEADER))
        assert payload is not None, "instrument() did not auto-wire the mint"
        assert payload.client_name == "Cursor"
        assert payload.client_version == "0.42"


# --- loud diagnostics for the silent "middleware never attached" failure -----
#
# The failure these guard: on a stateless server whose mint middleware never
# attached, every request falls back to a per-process session and `$session_id`
# fragments across pods with nothing in the SDK saying so. Two signals cover it --
# one at instrument() time, one on the first affected request.

# Substring unique to the runtime warning (the instrument-time one has its own).
_NO_SESSION = "no session id"
_WRONG_ORDER = "streamable_http_app() was called before instrument()"


class _Sink:
    def capture(self, *_: object, **__: object) -> None:
        pass


@contextmanager
def _captured_logs():
    """Route the SDK logger into a list for the duration of the block, then put
    back whatever sink was installed before -- `set_logger` is global process
    state, so restoring `None` unconditionally would silence a concurrent test."""
    logs: list[str] = []
    previous = logger_module._active_logger
    set_logger(logs.append)
    try:
        yield logs
    finally:
        set_logger(previous)


def _http_ctx(headers=None):
    """The per-request context shape callbacks receive as `extra["ctx"]` on an
    HTTP transport. `request=None` is how every SDK major represents stdio."""
    return SimpleNamespace(request=SimpleNamespace(headers=headers or {}))


def _stateless_server(name: str):
    """A real stateless streamable-HTTP FastMCP with one tool."""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    srv = FastMCP(
        name,
        stateless_http=True,
        json_response=True,
        # TestClient sends Host: testserver; allow it past DNS-rebinding protection.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )

    @srv.tool()
    def ping() -> str:
        return "pong"

    return srv


def _rpc(method, params=None, id=1, extra_headers=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **(extra_headers or {}),
    }
    body = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        body["params"] = params
    return headers, json.dumps(body)


def _call_ping(client, extra_headers=None, id=1):
    headers, body = _rpc(
        "tools/call",
        {"name": "ping", "arguments": {}},
        id=id,
        extra_headers=extra_headers,
    )
    return client.post("/mcp", headers=headers, content=body)


# --- runtime signal, end to end ----------------------------------------------


def test_runtime_warns_when_app_was_built_before_instrument():
    """The customer's ordering trap, reproduced against a real transport: the ASGI
    app is built before instrument(), so autowiring can't retrofit it and a real
    tools/call arrives with no session of any kind. Both signals must fire."""
    pytest.importorskip("starlette.testclient")
    pytest.importorskip("mcp.server.fastmcp")  # v1-only server; skipped under mcp>=2
    from starlette.testclient import TestClient

    from posthog.mcp import instrument

    srv = _stateless_server("posthog-ordering-trap")
    app = srv.streamable_http_app()  # BEFORE instrument() -- the trap

    with _captured_logs() as logs:
        instrument(srv, _Sink())
        with TestClient(app) as client:
            resp = _call_ping(client)
            assert resp.status_code == 200, resp.text

    assert [m for m in logs if _WRONG_ORDER in m], "no instrument-time warning"
    assert [m for m in logs if _NO_SESSION in m], "no runtime warning"


def test_runtime_silent_when_correctly_wired():
    """The regression that matters most: a correctly-ordered stateless server whose
    client replays the minted token must stay completely quiet. A diagnostic that
    cries wolf on healthy servers is worse than no diagnostic."""
    pytest.importorskip("starlette.testclient")
    pytest.importorskip("mcp.server.fastmcp")
    from starlette.testclient import TestClient

    from posthog.mcp import instrument

    srv = _stateless_server("posthog-correctly-wired")

    with _captured_logs() as logs:
        instrument(srv, _Sink())  # BEFORE building the app -- autowiring works
        app = srv.streamable_http_app()

        with TestClient(app) as client:
            headers, body = _rpc(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "Claude Code", "version": "9.9.9"},
                },
            )
            resp = client.post("/mcp", headers=headers, content=body)
            assert resp.status_code == 200, resp.text
            token = resp.headers.get(MCP_SESSION_HEADER)
            assert decode_session_id(token) is not None, "mint did not attach"

            # A compliant client replays the token on every subsequent request.
            resp2 = _call_ping(
                client,
                extra_headers={
                    MCP_SESSION_HEADER: token,
                    "mcp-protocol-version": "2025-06-18",
                },
                id=2,
            )
            assert resp2.status_code == 200, resp2.text

    assert not [m for m in logs if _WRONG_ORDER in m]
    assert not [m for m in logs if _NO_SESSION in m]


def test_warnings_are_visible_without_configuring_a_logger(caplog):
    """The point of the whole change. `log()` is a no-op unless the host passes
    MCPAnalyticsOptions(logger=...), so routing these warnings through it alone
    would leave the failure exactly as dark as it was -- the customer who lost
    weeks of sessions had no logger configured. They go to the `posthog.mcp`
    stdlib logger too, which a default-configured host actually sees."""
    pytest.importorskip("starlette.testclient")
    pytest.importorskip("mcp.server.fastmcp")
    from starlette.testclient import TestClient

    from posthog.mcp import instrument

    srv = _stateless_server("posthog-no-logger-configured")
    app = srv.streamable_http_app()

    set_logger(None)  # explicitly no `logger` option anywhere
    with caplog.at_level("WARNING", logger="posthog.mcp"):
        instrument(srv, _Sink())
        with TestClient(app) as client:
            _call_ping(client)

    messages = [r.getMessage() for r in caplog.records if r.name == "posthog.mcp"]
    assert [m for m in messages if _NO_SESSION in m], "runtime warning not visible"
    assert [m for m in messages if _WRONG_ORDER in m], "ordering warning not visible"


# --- runtime signal, predicate edges -----------------------------------------


async def test_no_warning_for_stdio():
    """stdio carries no request at all, so a per-process session is correct there.
    Warning would be pure noise on the most common local-dev path."""
    with _captured_logs() as logs:
        data = _data()
        await prepare_request(
            data,
            mcp_session_id=None,
            client_name=None,
            client_version=None,
            request={"method": "tools/call", "params": {}},
            extra={"ctx": SimpleNamespace(request=None)},
        )

    assert not [m for m in logs if _NO_SESSION in m]
    assert data.warned_no_stateless_session is False


async def test_no_warning_when_session_is_anchored_by_conversation_id():
    """A conversation-anchored session is derived deterministically and agrees
    across pods, so it does not fragment -- no warning, even though the request is
    HTTP and carries no session header.

    Regression test for a real trap: `resolve_session_id` returns early on this
    path and never writes `data.session_source`, so a check that read that shared
    field afterwards would see a stale "generated" and warn about a session that
    is perfectly healthy."""
    with _captured_logs() as logs:
        data = _data()
        session_id = await prepare_request(
            data,
            mcp_session_id=None,
            client_name=None,
            client_version=None,
            request={"method": "tools/call", "params": {}},
            extra={"ctx": _http_ctx()},
            conversation_id="0199e0a1-0000-7000-8000-000000000000",
        )

    assert session_id.startswith("ses_")
    assert not [m for m in logs if _NO_SESSION in m]


async def test_no_warning_for_sse_transport():
    """The deprecated SSE transport carries its session as a query param, and the
    mint sets a response header an SSE client never replays -- so the middleware we
    would be recommending cannot help. Stay quiet rather than give wrong advice."""
    ctx = _http_ctx()
    ctx.request.query_params = {"session_id": "sse-session-1"}

    with _captured_logs() as logs:
        await prepare_request(
            _data(),
            mcp_session_id=None,
            client_name=None,
            client_version=None,
            request={"method": "tools/call", "params": {}},
            extra={"ctx": ctx},
        )

    assert not [m for m in logs if _NO_SESSION in m]


async def test_runtime_warning_fires_once_per_server():
    """Warn-once: a busy server must not write this line on every request."""
    with _captured_logs() as logs:
        data = _data()
        for _ in range(3):
            await prepare_request(
                data,
                mcp_session_id=None,
                client_name=None,
                client_version=None,
                request={"method": "tools/call", "params": {}},
                extra={"ctx": _http_ctx()},
            )

    warnings = [m for m in logs if _NO_SESSION in m]
    assert len(warnings) == 1
    assert "add_middleware(PostHogMcpStatelessSessionMiddleware)" in warnings[0]
    assert data.warned_no_stateless_session is True


# --- instrument-time signal ---------------------------------------------------


def test_no_instrument_warning_when_app_not_yet_built():
    """The correctly-ordered path: instrument() on a server whose app has never
    been built has nothing to complain about."""
    pytest.importorskip("mcp.server.fastmcp")

    from posthog.mcp import instrument

    srv = _stateless_server("posthog-app-not-built")

    logs: list[str] = []
    instrument(srv, _Sink(), MCPAnalyticsOptions(logger=logs.append))
    set_logger(None)  # instrument() installs the option globally; undo it

    assert not [m for m in logs if _WRONG_ORDER in m]


def _server_for_installed_major(name: str):
    """A streamable-HTTP server built with whichever MCP SDK major is installed."""
    if MCP_MAJOR < 2:
        from mcp.server.fastmcp import FastMCP

        return FastMCP(name, stateless_http=True)
    from mcp.server.mcpserver import MCPServer

    return MCPServer(name)


def test_app_built_probe_fires_on_the_installed_sdk_major():
    """``_app_was_already_built`` must flip once the app exists -- on *both* MCP
    majors, which is why this runs unguarded on whichever one is installed.

    The attribute holding the low-level server was renamed across the major
    boundary (``_mcp_server`` on 1.x's FastMCP, ``_lowlevel_server`` on 2.x's
    MCPServer). A probe that knows only one name still passes every 1.x test
    while silently never firing on 2.x, so the instrument-time warning goes dark
    on exactly one half of the matrix with nothing failing to say so."""
    srv = _server_for_installed_major("posthog-probe-major")

    assert _app_was_already_built(srv) is False, "probe fired before the app existed"
    srv.streamable_http_app()
    assert _app_was_already_built(srv) is True, (
        f"probe blind to a built app on MCP {MCP_MAJOR}.x -- the ordering warning "
        "cannot fire for these servers"
    )
