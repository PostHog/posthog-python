# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Read HTTP request headers inside a host callback, on either MCP SDK major.

``identify``, ``intent_fallback``, ``event_properties`` and ``before_send``
receive the SDK's own per-request context under ``extra["ctx"]``, unchanged. We
deliberately do not synthesise a uniform shape for it: the two majors expose
different objects, and a fabricated one is a convincing partial lie about a
shape the SDK actually changed. Headers are the one thing nearly every callback
wants, so they get a helper instead::

    from posthog.mcp import get_request_headers

    def identify(request, extra):
        headers = get_request_headers(extra) or {}
        token = headers.get("authorization")
        ...

Returns ``None`` when the request did not arrive over HTTP — stdio and
in-memory transports carry no headers at all.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

__all__ = ["get_request_headers"]

RequestHeaderBag = Dict[str, str]


def get_request_headers(extra: Any) -> Optional[RequestHeaderBag]:
    """The request's HTTP headers as a plain dict with lowercase keys, or ``None``.

    Accepts the ``extra`` dict handed to a callback, or the raw per-request
    context itself, so it works whichever one a host happens to hold.
    """
    ctx = extra
    if isinstance(extra, dict):
        ctx = extra.get("ctx")
    if ctx is None:
        return None

    # Both majors reach the transport's request the same way from their own
    # context object (`ServerRequestContext` on 2.x, `RequestContext` on 1.x);
    # `request` is None on stdio.
    source = getattr(getattr(ctx, "request", None), "headers", None)
    if source is None:
        return None
    return _to_header_bag(source)


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
