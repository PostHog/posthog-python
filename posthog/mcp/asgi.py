"""Frictionless session-token minting for stateless / multi-pod MCP servers.

A stateless MCP server issues no session id, so ``$session_id`` fragments across
pods and the client identity (the "harness") sent only at ``initialize`` is lost.
The fix is a self-encoded token (see :mod:`.session_token`) minted onto the
``Mcp-Session-Id`` response header at ``initialize``; clients replay it on every
request, so any pod recovers session + harness from the header alone.

The MCP Python SDK owns ``initialize`` in its runner/session layer and forbids
overriding it via ``request_handlers``, so -- unlike the TS SDK, which wraps the
initialize handler -- there is no in-SDK seam to mint from. We mint at the HTTP
layer instead, with a pure-ASGI middleware that works for both a mounted FastMCP
app and a custom ``PostHogMCP`` dispatcher, across every SDK routing path.

On the ``instrument()`` path this is **wired up automatically**:
``instrument(server, ...)`` wraps the FastMCP server's app factories
(``streamable_http_app()`` / ``sse_app()``, which ``mcp.run()`` also calls), so the
app it builds already carries the middleware -- nothing extra to add.

For a custom ``PostHogMCP`` dispatcher (you own the ASGI app), add it once::

    app.add_middleware(PostHogMcpStatelessSessionMiddleware)

then read the recovered session on any request::

    sess = get_mcp_session(request)
    posthog.capture_tool_call(
        tool_name=name,
        session_id=sess.session_id if sess else None,
        client_name=sess.client_name if sess else None,
        client_version=sess.client_version if sess else None,
    )

This module has no hard dependency on Starlette/FastAPI -- it speaks raw ASGI.
"""

from __future__ import annotations

import functools
import json
from typing import Any, Optional

from .logger import log
from .session import new_session_id
from .session_token import (
    MCP_SESSION_HEADER,
    SessionTokenPayload,
    decode_session_id,
    encode_session_id,
    read_mcp_session_header,
)

# Scope key the middleware stashes the decoded token payload under.
_SCOPE_KEY = "posthog_mcp_session"

# JSON-RPC request bodies are tiny; cap what we buffer so a stray large POST on
# the same app can't be read into memory in full before minting.
_MAX_SNIFF_BODY = 256 * 1024


def get_mcp_session(request_or_scope: Any) -> Optional[SessionTokenPayload]:
    """Return the ``SessionTokenPayload`` the middleware recovered for this request,
    or ``None``. Accepts a Starlette ``Request`` or a raw ASGI ``scope`` dict."""
    scope = getattr(request_or_scope, "scope", request_or_scope)
    if not isinstance(scope, dict):
        return None
    value = scope.get(_SCOPE_KEY)
    return value if isinstance(value, SessionTokenPayload) else None


class PostHogMcpStatelessSessionMiddleware:
    """ASGI middleware that mints a session token onto the ``Mcp-Session-Id``
    response header at ``initialize`` (when the client sent none) and decodes the
    replayed token on every request, exposing it via :func:`get_mcp_session`.

    Fail-safe: any error while sniffing/minting is logged and the request passes
    through untouched -- analytics must never break the host."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        try:
            incoming_header = read_mcp_session_header(_headers_dict(scope))
        except Exception as error:  # noqa: BLE001
            log(f"PostHog MCP session middleware: header read failed - {error}")
            await self.app(scope, receive, send)
            return

        # Decode a replayed token (if any) so the app can read it via get_mcp_session.
        decoded = decode_session_id(incoming_header)
        if decoded is not None:
            scope[_SCOPE_KEY] = decoded

        # Only POSTs carry JSON-RPC. Mint only when the client replayed no session
        # id at all -- if one is present (ours or a stateful transport's), leave it.
        if scope.get("method") != "POST" or incoming_header is not None:
            await self.app(scope, receive, send)
            return

        body, receive = await _buffer_body(receive)
        token = _mint_token_if_initialize(body)
        if token is None:
            await self.app(scope, receive, send)
            return

        scope[_SCOPE_KEY] = decode_session_id(token)
        await self.app(scope, receive, _sending_session_header(send, token))


def _headers_dict(scope: Any) -> dict[str, str]:
    """ASGI raw headers (list of (bytes, bytes)) -> a lowercased str dict."""
    result: dict[str, str] = {}
    for key, value in scope.get("headers") or []:
        try:
            result[key.decode("latin-1").lower()] = value.decode("latin-1")
        except Exception:  # noqa: BLE001
            continue
    return result


async def _buffer_body(receive: Any) -> tuple[bytes, Any]:
    """Read the full request body, then return it plus a ``receive`` that replays
    it downstream (so the app still sees an unconsumed stream)."""
    chunks: list[bytes] = []
    more = True
    while more:
        message = await receive()
        if message.get("type") != "http.request":
            # A non-body message (e.g. http.disconnect); stop and replay what we have.
            break
        chunks.append(message.get("body", b"") or b"")
        more = message.get("more_body", False)

    # Always buffer the whole body so replay is byte-faithful (Starlette's own
    # Request.body() buffers fully too). This holds the request in memory, which
    # is fine for tiny JSON-RPC POSTs on an MCP endpoint.
    buffered = b"".join(chunks)
    replayed = False

    async def replay() -> dict[str, Any]:
        # Hand the whole buffered body back in one message, then defer to the
        # original transport for anything after it (disconnect, etc.).
        nonlocal replayed
        if not replayed:
            replayed = True
            return {"type": "http.request", "body": buffered, "more_body": False}
        return await receive()

    # Only *sniff* (parse to detect initialize) small bodies -- a giant POST is
    # never our tiny initialize handshake, so skip minting but still replay it whole.
    sniff = buffered if len(buffered) <= _MAX_SNIFF_BODY else b""
    return sniff, replay


def _mint_token_if_initialize(body: bytes) -> Optional[str]:
    """If ``body`` is an ``initialize`` JSON-RPC request, mint and return a token
    string carrying a fresh session id + the client's self-reported identity.
    Returns ``None`` for anything else (never raises)."""
    if not body:
        return None
    try:
        message = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(message, dict) or message.get("method") != "initialize":
        return None
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    client_info = params.get("clientInfo")
    client_info = client_info if isinstance(client_info, dict) else {}
    try:
        return encode_session_id(
            SessionTokenPayload(
                session_id=new_session_id(),
                client_name=_str_or_none(client_info.get("name")),
                client_version=_str_or_none(client_info.get("version")),
                protocol_version=_str_or_none(params.get("protocolVersion")),
            )
        )
    except Exception as error:  # noqa: BLE001
        log(f"PostHog MCP session middleware: mint failed - {error}")
        return None


def _sending_session_header(send: Any, token: str) -> Any:
    """Wrap ``send`` so the outgoing response start carries ``Mcp-Session-Id: token``
    -- but only if the app/transport didn't already set one (never clobber a
    stateful transport's own session id)."""
    header = MCP_SESSION_HEADER.encode("latin-1")
    token_bytes = token.encode("latin-1")

    async def wrapped(message: dict[str, Any]) -> None:
        if message.get("type") == "http.response.start":
            headers = list(message.get("headers") or [])
            if not any(k.lower() == header for k, _ in headers):
                headers.append((header, token_bytes))
                message = {**message, "headers": headers}
        await send(message)

    return wrapped


def _str_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


# Marker so we never double-wrap a factory (idempotent across repeat instrument()).
_AUTOWIRED = "__posthog_mcp_autowired__"


def autowire_stateless_mint(server: Any) -> None:
    """Make stateless minting zero-config on the ``instrument()`` path.

    Wraps a FastMCP server's ASGI-app factories so the app they build already has
    :class:`PostHogMcpStatelessSessionMiddleware` applied. Covers both
    ``server.streamable_http_app()`` / ``sse_app()`` and ``mcp.run(transport=...)``
    (which calls those factories internally), so the user adds nothing.

    No-op for servers without app factories (stdio / low-level ``Server``), and safe
    if the middleware is also added manually (it only mints when none is present)."""
    for attr in ("streamable_http_app", "sse_app", "http_app"):
        original = getattr(server, attr, None)
        if not callable(original) or getattr(original, _AUTOWIRED, False):
            continue
        try:
            setattr(server, attr, _wrap_app_factory(original))
        except Exception as error:  # noqa: BLE001 - never let wiring break instrument()
            log(f"PostHog MCP: could not auto-wire stateless mint on {attr} - {error}")


def _wrap_app_factory(original: Any) -> Any:
    @functools.wraps(original)
    def factory(*args: Any, **kwargs: Any) -> Any:
        app = original(*args, **kwargs)
        add_middleware = getattr(app, "add_middleware", None)
        if callable(add_middleware):
            add_middleware(PostHogMcpStatelessSessionMiddleware)
            return app
        # Not a Starlette app -- wrap as raw ASGI so minting still happens.
        return PostHogMcpStatelessSessionMiddleware(app)

    setattr(factory, _AUTOWIRED, True)
    return factory
