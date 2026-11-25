# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Read HTTP request headers inside a host callback, on either MCP SDK major.

``identify``, ``intent_fallback`` and ``event_properties`` receive the SDK's
own per-request context under ``extra["ctx"]``, unchanged. (``before_send``
does not: it is handed the finished capture payload, not the request.) We
deliberately do not synthesise a uniform shape for it: the two majors expose
different objects, and a fabricated one is a convincing partial lie about a
shape the SDK actually changed. Headers are the one thing nearly every callback
wants, so they get a helper instead::

    from posthog.mcp import get_request_headers

    def identify(request, extra):
        headers = get_request_headers(extra) or {}
        return UserIdentity(distinct_id=user_id_for(headers.get("authorization")))

**Never return a raw header value** (an ``Authorization`` bearer token, a
cookie, an API key) as a ``UserIdentity`` property or from ``event_properties``.
Resolve it to an id or a role first. Person properties and custom event
properties are the one part of the payload the SDK does not redact, and they
persist on the person profile — a token written there outlives event retention
and is visible in the PostHog UI.

Returns ``None`` when the request did not arrive over HTTP — stdio and
in-memory transports carry no headers at all.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .constants import SESSIONLESS_PROTOCOL_VERSION
from .session_token import read_mcp_session_header

__all__ = ["get_request_headers"]

RequestHeaderBag = Dict[str, str]


def get_request(extra: Any) -> Optional[Any]:
    """The transport's per-request object (Starlette ``Request`` or equivalent)
    underneath ``extra``, or ``None`` on stdio / in-memory transports.

    Accepts the ``extra`` dict handed to a callback, or the raw per-request
    context itself, so it works whichever one a host happens to hold. Both SDK
    majors reach it the same way from their own context object
    (``ServerRequestContext`` on 2.x, ``RequestContext`` on 1.x).

    Shared by anything that needs to read the request beyond just its headers
    (e.g. query params) -- one place that knows how to unwrap ``extra``/``ctx``
    down to the request, instead of each caller re-deriving it.
    """
    ctx = _request_context(extra)
    if ctx is None:
        return None
    return getattr(ctx, "request", None)


def get_request_headers(extra: Any) -> Optional[RequestHeaderBag]:
    """The request's HTTP headers as a plain dict with lowercase keys, or ``None``.

    Accepts the ``extra`` dict handed to a callback, or the raw per-request
    context itself, so it works whichever one a host happens to hold.
    """
    # `request` is None on stdio.
    source = getattr(get_request(extra), "headers", None)
    if source is None:
        return None
    return _to_header_bag(source)


def read_transport_session_id(extra: Any, *, handshake: bool = False) -> Optional[str]:
    """The SSE transport checks the ``session_id`` query parameter against its open
    streams, while a stateless request never completes a handshake, so adding the
    parameter to a request picks no session."""
    header = read_mcp_session_header(get_request_headers(extra))
    if header is not None:
        return header
    ctx = _request_context(extra)
    try:
        if not (handshake or _completed_a_handshake(ctx)):
            return None
        params = getattr(get_request(ctx), "query_params", None)
        value = params.get("session_id") if params is not None else None
    except Exception:  # noqa: BLE001 - a transport probe must never break a tool call
        return None
    return value if isinstance(value, str) and value else None


def _completed_a_handshake(ctx: Any) -> bool:
    params = getattr(getattr(ctx, "session", None), "client_params", None)
    if params is None:
        return False
    # mcp 2.x builds client params from a 2026-07-28 request's own envelope.
    version = getattr(params, "protocol_version", None) or getattr(
        params, "protocolVersion", None
    )
    return version != SESSIONLESS_PROTOCOL_VERSION


def _request_context(extra: Any) -> Any:
    return extra.get("ctx") if isinstance(extra, dict) else extra


def _to_header_bag(source: Any) -> Optional[RequestHeaderBag]:
    """Flatten a Starlette ``Headers``, a mapping, or anything iterable of pairs
    into a lowercase-keyed dict. Never raises: a header read must not take a
    tool call down with it."""
    try:
        # Starlette's Headers and dict both expose .items(); Headers already
        # lowercases, a plain dict may not, so normalise either way.
        items = source.items() if hasattr(source, "items") else source
        bag: RequestHeaderBag = {}
        for key, value in items:
            if isinstance(key, str) and isinstance(value, str):
                bag[key.lower()] = value
        return bag
    except Exception:  # noqa: BLE001 - best effort, never break the tool path
        return None
