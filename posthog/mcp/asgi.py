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

The same blind spot hides the handshake from analytics on MCP SDK 1.x, so a
middleware bound to an instrumented server also captures ``$mcp_initialize`` here,
once per ``initialize``, in the session the response hands the client. (MCP SDK
2.x exposes ``Server.middleware``, which ``instrument()`` uses instead.)

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
import weakref
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import parse_qs

from ._instrumentation import HANDSHAKE_OBSERVER_KEY, record_initialize
from ._internal import MCPAnalyticsData, get_server_tracking_data, tracking_key
from .logger import log, warn
from .session import handshake_session_id, new_session_id
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

    Pass the instrumented ``server`` to also capture ``$mcp_initialize`` from the
    handshake itself on MCP SDK 1.x, which answers ``initialize`` outside anything
    ``instrument()`` can wrap. ``instrument()`` does this for the apps it autowires;
    add it yourself when you mount the middleware by hand on an instrumented server.
    Leave it out for a custom ``PostHogMCP`` dispatcher, which captures its own
    events.

    Fail-safe: any error while sniffing/minting is logged and the request passes
    through untouched -- analytics must never break the host."""

    def __init__(self, app: Any, server: Any = None) -> None:
        self.app = app
        self._server: Optional[weakref.ReferenceType[Any]] = None
        if server is not None:
            try:
                self._server = weakref.ref(tracking_key(server))
            except TypeError as error:
                log(f"PostHog MCP session middleware: cannot bind server= - {error}")

    def _analytics(self) -> Optional[MCPAnalyticsData]:
        """The bound server's tracking state, when this middleware is the one that
        captures its handshakes: on mcp 2.x ``Server.middleware`` already does."""
        try:
            server = self._server() if self._server is not None else None
            data = get_server_tracking_data(server) if server is not None else None
        except TypeError:  # an unhashable server= can never be instrumented
            return None
        return None if data is None or data.handshake_observed_in_process else data

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        # Tell the in-process fallback that this request's handshake is captured
        # here. Two stacked middlewares see the same request; the outer one owns it.
        # The SSE transport stays unmarked: its session is in a query parameter, so
        # its first request captures the handshake, in the session its calls use.
        observer = None
        if HANDSHAKE_OBSERVER_KEY not in scope and not _is_sse(scope):
            observer = self._analytics()
            if observer is not None:
                scope[HANDSHAKE_OBSERVER_KEY] = observer

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
        # Such a request is not read, so an initialize that reuses a session id is
        # not captured here. Tool calls, which all carry one, stay unread.
        if scope.get("method") != "POST" or incoming_header is not None:
            await self.app(scope, receive, send)
            return

        # Guard the whole sniff+mint path: on any failure (a raising receive() or
        # adversarial body) the request passes through untouched, per the contract.
        token = None
        initialize = None
        try:
            body, receive = await _buffer_body(receive)
            initialize = _parse_initialize(body)
            if initialize is not None:
                token = _mint_token(initialize)
        except Exception as error:  # noqa: BLE001
            log(f"PostHog MCP session middleware: mint failed - {error}")

        if initialize is None:
            await self.app(scope, receive, send)
            return

        started = datetime.now(timezone.utc)
        if observer is not None:
            send = _InitializeResponse(
                send,
                functools.partial(
                    _record_initialize, observer, initialize, scope, started
                ),
            )
        if token is not None:
            scope[_SCOPE_KEY] = decode_session_id(token)
            send = _sending_session_header(send, token)

        await self.app(scope, receive, send)


def _is_sse(scope: Any) -> bool:
    """The deprecated SSE transport, which keys its session on ``?session_id=``."""
    try:
        query = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
    except Exception:  # noqa: BLE001
        return False
    return "session_id" in query


def _headers_dict(scope: Any) -> dict[str, str]:
    """ASGI raw headers (list of (bytes, bytes)) -> a lowercased str dict."""
    return _decode_headers(scope.get("headers") or [])


def _decode_headers(raw: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in raw:
        try:
            result[key.decode("latin-1").lower()] = value.decode("latin-1")
        except Exception:  # noqa: BLE001
            continue
    return result


async def _buffer_body(receive: Any) -> tuple[bytes, Any]:
    """Read up to ``_MAX_SNIFF_BODY`` of the request body for sniffing, then return
    that prefix plus a ``receive`` that replays it and streams the rest.

    Memory is bounded to the cap: an `initialize` handshake is tiny, so once we
    exceed the cap the request cannot be one and we stop buffering — the prefix is
    replayed with ``more_body: True`` and any remaining chunks are forwarded
    straight from the original ``receive`` (never accumulated). This keeps an
    unauthenticated large / streamed POST from exhausting memory."""
    chunks: list[bytes] = []
    total = 0
    complete = False
    overflow = False
    # A non-body message (e.g. http.disconnect) ends the read early. It is kept
    # and handed to the app after the prefix, so the app sees the disconnect.
    held: Optional[dict[str, Any]] = None
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            held = message
            break
        chunks.append(message.get("body", b"") or b"")
        total += len(chunks[-1])
        if not message.get("more_body", False):
            complete = True
            break
        if total > _MAX_SNIFF_BODY:
            overflow = True  # too big to be initialize — stop buffering
            break

    buffered = b"".join(chunks)
    replayed = False

    async def replay() -> dict[str, Any]:
        # Replay the buffered prefix once; if we stopped early, flag more_body so
        # the app keeps reading: the held message, then the original transport.
        nonlocal replayed, held
        if not replayed:
            replayed = True
            return {"type": "http.request", "body": buffered, "more_body": not complete}
        if held is not None:
            message, held = held, None
            return message
        return await receive()

    # Only sniff (parse for `initialize`) when we captured the whole small body.
    sniff = buffered if (complete and not overflow) else b""
    return sniff, replay


def _parse_initialize(body: bytes) -> Optional[Dict[str, Any]]:
    """The JSON-RPC message in ``body`` if it is an ``initialize`` request, else
    ``None`` (never raises)."""
    if not body:
        return None
    try:
        # RecursionError (deeply-nested JSON) is a RuntimeError, not a
        # ValueError/TypeError -- catch broadly so a hostile body can't escape.
        message = json.loads(body)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(message, dict) or message.get("method") != "initialize":
        return None
    return message


def _mint_token(message: Dict[str, Any]) -> Optional[str]:
    """Mint a token carrying a fresh session id + the client's self-reported
    identity from an ``initialize`` message. Returns ``None`` on failure."""
    params, client_info = _initialize_params(message)
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


def _initialize_params(
    message: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    client_info = params.get("clientInfo")
    return params, client_info if isinstance(client_info, dict) else {}


class _InitializeResponse:
    """A ``send`` that records the handshake when the ``initialize`` response
    starts, then passes the response on. The app calls it from inside its own
    request context, so an auth middleware's user is still set for ``identify``.
    The start carries the status and the ``Mcp-Session-Id`` the client will send
    back (whichever of the transport or the mint set it). The body is not read."""

    def __init__(self, send: Any, record: Any) -> None:
        self._send = send
        self._record = record

    async def __call__(self, message: Dict[str, Any]) -> None:
        if message.get("type") == "http.response.start" and self._record is not None:
            record, self._record = self._record, None
            headers = _decode_headers(message.get("headers") or [])
            await record(
                message.get("status"), headers.get(MCP_SESSION_HEADER.lower()) or None
            )
        await self._send(message)


class _InitializeContext:
    """``extra["ctx"]`` for callbacks run on an ``initialize`` observed here.

    MCP SDK 1.x answers ``initialize`` inside its session layer, outside any request
    context, so there is no SDK context object to hand over. This carries only the
    HTTP request, which is what :func:`~posthog.mcp.get_request_headers` reads."""

    def __init__(self, request: Any) -> None:
        self.request = request


async def _record_initialize(
    data: MCPAnalyticsData,
    message: Dict[str, Any],
    scope: Any,
    started: datetime,
    status: Optional[int],
    session_header: Optional[str],
) -> None:
    """Emit ``$mcp_initialize`` for a handshake the app answered, in the session the
    client's later requests resolve to: the ``Mcp-Session-Id`` they send back (a
    stateful transport's id or our token), or this process's own session when the
    response carried none. The body is not read, so the
    event carries the protocol version the client asked for."""
    try:
        if status is None or not 200 <= status < 300:
            return
        from starlette.requests import Request

        header = session_header
        await record_initialize(
            data,
            session_id=await handshake_session_id(
                data, header, decode_session_id(header)
            ),
            params=_initialize_params(message)[0],
            result=None,
            extra={"session_id": header, "ctx": _InitializeContext(Request(scope))},
            timestamp=started,
        )
    except Exception as error:  # noqa: BLE001 - analytics must never break the host
        log(
            f"PostHog MCP session middleware: failed to capture $mcp_initialize - {error}"
        )


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
    if the middleware is also added manually (it only mints when none is present).
    On fastmcp 2.x, ``streamable_http_app`` / ``sse_app`` can be thin wrappers over
    ``http_app``; wrapping all three could add the middleware twice to one app, so
    the factory guards against a double-add (see ``_app_already_wrapped``)."""
    _warn_if_app_built_before_instrument(server)
    for attr in ("streamable_http_app", "sse_app", "http_app"):
        original = getattr(server, attr, None)
        if not callable(original) or getattr(original, _AUTOWIRED, False):
            continue
        try:
            setattr(server, attr, _wrap_app_factory(original, server))
        except Exception as error:  # noqa: BLE001 - never let wiring break instrument()
            log(f"PostHog MCP: could not auto-wire stateless mint on {attr} - {error}")


def _app_was_already_built(server: Any) -> bool:
    """Whether the streamable-HTTP app already exists, so wrapping the factories
    now cannot retrofit it.

    The tell is ``_session_manager``, created lazily on the first
    ``streamable_http_app()`` call and non-``None`` forever after. It sits on the
    server itself on the official SDK's ``FastMCP`` (1.x) and on the low-level
    server it delegates to (2.x's ``MCPServer`` renamed that attribute
    ``_lowlevel_server``; older/other wrappers may still use ``_mcp_server``), so
    check both names.

    Deliberately partial: jlowin's ``fastmcp`` 2.x/3.x keeps its session manager as
    a local inside ``http_app()`` and never stores it, so there is nothing to probe
    and those servers get no instrument-time warning. The runtime warning in
    ``_instrumentation`` still covers them."""
    low_level = getattr(server, "_mcp_server", None) or getattr(
        server, "_lowlevel_server", None
    )
    for candidate in (server, low_level):
        try:
            if getattr(candidate, "_session_manager", None) is not None:
                return True
        except Exception:  # noqa: BLE001 - never let a probe break instrument()
            continue
    return False


def _warn_if_app_built_before_instrument(server: Any) -> None:
    """Catch the ordering trap that silently disables stateless capture: the
    streamable-HTTP app was built (and likely already mounted) *before* ``instrument()``
    ran, so wrapping the factories now can't retrofit that already-built app."""
    if not _app_was_already_built(server):
        return
    warn(
        "Warning: streamable_http_app() was called before instrument(), so the ASGI app "
        "already in use has no PostHog MCP middleware and stateless sessions will not be "
        "captured (autowiring only affects apps built after instrument() runs). Call "
        "instrument(server) before building or mounting the app, or add the middleware "
        "manually: app.add_middleware(PostHogMcpStatelessSessionMiddleware). "
        "You can ignore this if you already added the middleware yourself — the app is "
        "built by then, so there is no way for us to tell from here. "
        "See posthog/mcp/README.md (stateless / multi-pod servers)."
    )


def _app_already_wrapped(app: Any) -> bool:
    """True if ``app`` already carries our middleware -- so wrapping a factory that
    delegates to another wrapped factory (fastmcp 2.x aliases) doesn't add it twice."""
    if isinstance(app, PostHogMcpStatelessSessionMiddleware):
        return True
    for middleware in getattr(app, "user_middleware", None) or []:
        if getattr(middleware, "cls", None) is PostHogMcpStatelessSessionMiddleware:
            return True
    return False


def _wrap_app_factory(original: Any, server: Any = None) -> Any:
    @functools.wraps(original)
    def factory(*args: Any, **kwargs: Any) -> Any:
        app = original(*args, **kwargs)
        if _app_already_wrapped(app):
            return app
        add_middleware = getattr(app, "add_middleware", None)
        if callable(add_middleware):
            add_middleware(PostHogMcpStatelessSessionMiddleware, server=server)
            return app
        # Not a Starlette app -- wrap as raw ASGI so minting still happens.
        return PostHogMcpStatelessSessionMiddleware(app, server=server)

    setattr(factory, _AUTOWIRED, True)
    return factory
