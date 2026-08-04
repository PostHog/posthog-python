# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""mcp 2.x (2026-07-28 spec) adapter.

The v2 SDK dropped the ``request_handlers`` monkey-patch seam the v1 adapters use
and added an official context-tier middleware protocol (``ServerMiddleware``): a
``(ctx, call_next)`` callable appended to ``server.middleware`` that wraps every
inbound request. We attach one such middleware instead of patching internals.

Unlike v1, the middleware observes results as already-serialized wire dicts
(``{"content": [...], "isError": ...}`` for ``tools/call``, ``{"tools": [...]}``
for ``tools/list``) or a raised ``MCPError`` on failure. Per-request client
identity and protocol version ride the ``_meta`` envelope
(``io.modelcontextprotocol/clientInfo`` etc.) on 2026-07-28 sessions, exposed on
``ctx.meta``; a client that negotiated an older protocol against this SDK sends
no envelope, so client info is recovered from the ``initialize`` params instead.

This adapter is capture-only: it does not inject a ``context`` parameter, mutate
the tool list (no ``get_more_tools``), or stitch MRTR round-trips. An
``input_required`` interim result is stamped with ``$mcp_result_type`` and is
never treated as an error.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

from ._instrumentation import (
    build_tool_call_request,
    prepare_request,
    record_tool_call,
    record_tools_list,
)
from ._internal import MCPAnalyticsData

_MIDDLEWARE_FLAG = "__posthog_mcp_v2_middleware__"


def instrument_mcpserver_v2(server: Any, data: MCPAnalyticsData) -> None:
    """Instrument the mcp 2.x high-level ``MCPServer`` by attaching the analytics
    middleware to its (low-level-backed) ``middleware`` list."""
    low_level = getattr(server, "_lowlevel_server", None)
    data.server_name = getattr(server, "name", None) or getattr(low_level, "name", None)
    data.server_version = getattr(server, "version", None) or getattr(
        low_level, "version", None
    )
    _attach_middleware(server, data)


def instrument_low_level_v2(server: Any, data: MCPAnalyticsData) -> None:
    """Instrument a raw mcp 2.x ``mcp.server.lowlevel.Server`` via its public
    ``middleware`` list (the same seam ``MCPServer`` exposes)."""
    data.server_name = getattr(server, "name", None)
    data.server_version = getattr(server, "version", None)
    _attach_middleware(server, data)


def _attach_middleware(server: Any, data: MCPAnalyticsData) -> None:
    """Append the analytics ``ServerMiddleware`` to ``server.middleware``.

    Both ``MCPServer`` (via a property) and the low-level ``Server`` expose the
    same ``middleware`` list; appending is the official attach mechanism (no
    private-attr patching). Idempotent: a flagged middleware is never added twice.
    """
    middleware = getattr(server, "middleware", None)
    if middleware is None or not hasattr(middleware, "append"):
        raise TypeError(
            "mcp 2.x server exposes no `middleware` list to attach analytics to; "
            f"got {type(server)!r}. Supported: mcp>=2,<3 MCPServer / lowlevel Server."
        )
    if any(getattr(mw, _MIDDLEWARE_FLAG, False) for mw in middleware):
        return
    middleware.append(_build_middleware(data))


def _build_middleware(data: MCPAnalyticsData) -> Any:
    async def analytics_middleware(ctx: Any, call_next: Any) -> Any:
        # Observe only the methods we capture; everything else passes straight
        # through so we never alter dispatch for other requests or notifications.
        method = getattr(ctx, "method", None)
        if method == "tools/call":
            return await _on_tool_call(data, ctx, call_next)
        if method == "tools/list":
            return await _on_tools_list(data, ctx, call_next)
        if method == "server/discover":
            # A discover carries client info in its envelope but no tool payload;
            # emit the lazy $mcp_initialize (and identify) off it, then continue.
            await _prepare(data, ctx, {"method": "server/discover", "params": {}})
            return await call_next(ctx)
        return await call_next(ctx)

    setattr(analytics_middleware, _MIDDLEWARE_FLAG, True)
    return analytics_middleware


def _client_info(ctx: Any) -> Tuple[Optional[str], Optional[str]]:
    """Client name/version from the 2026-07-28 ``_meta`` envelope, or from an
    ``initialize`` request's params when an older-protocol session sends no
    envelope. Best-effort — returns ``(None, None)`` when neither is present."""
    meta = getattr(ctx, "meta", None)
    if isinstance(meta, dict):
        try:
            from mcp_types import CLIENT_INFO_META_KEY

            info = meta.get(CLIENT_INFO_META_KEY)
        except ImportError:
            info = None
        if isinstance(info, dict):
            name = info.get("name")
            version = info.get("version")
            if name or version:
                return name, version
    # Legacy handshake on the v2 SDK: `initialize` params carry clientInfo.
    params = getattr(ctx, "params", None)
    if isinstance(params, dict):
        info = params.get("clientInfo")
        if isinstance(info, dict):
            return info.get("name"), info.get("version")
    return None, None


def _protocol_version(ctx: Any) -> Optional[str]:
    version = getattr(ctx, "protocol_version", None)
    if isinstance(version, str) and version:
        return version
    meta = getattr(ctx, "meta", None)
    if isinstance(meta, dict):
        try:
            from mcp_types import PROTOCOL_VERSION_META_KEY

            value = meta.get(PROTOCOL_VERSION_META_KEY)
        except ImportError:
            value = None
        if isinstance(value, str) and value:
            return value
    return None


async def _prepare(
    data: MCPAnalyticsData, ctx: Any, request: Dict[str, Any]
) -> Tuple[str, str, Optional[str], Optional[str], Optional[str]]:
    """Resolve session (emitting identify + lazy initialize) for this request.

    v2 traffic has no self-encoded token and no ``Mcp-Session-Id`` header by
    construction (SEP-2567 removed it), so ``mcp_session_id``/``token`` are always
    None here; sessions come out ``derived`` (Stage C) when identified,
    ``generated`` otherwise. Returns the session id, its provenance source, and the
    resolved client/protocol info so the caller stamps them on the captured event."""
    client_name, client_version = _client_info(ctx)
    protocol_version = _protocol_version(ctx)
    extra = {"session_id": None}
    session_id, session_id_source = await prepare_request(
        data,
        mcp_session_id=None,
        client_name=client_name,
        client_version=client_version,
        protocol_version=protocol_version,
        request=request,
        extra=extra,
    )
    return session_id, session_id_source, client_name, client_version, protocol_version


def _result_type(result: Any) -> Optional[str]:
    """The 2026-07-28 ``resultType`` of a wire result, when non-default. Returns
    None for a plain "complete" result so we only stamp interesting values."""
    value: Any = None
    if isinstance(result, dict):
        value = result.get("resultType")
    else:
        value = getattr(result, "result_type", None)
    if isinstance(value, str) and value and value != "complete":
        return value
    return None


def _is_error_result(result: Any) -> bool:
    """A v2 tool result signals a tool error via ``isError``/``is_error`` — but an
    ``input_required`` interim result (MRTR) is NOT an error, just a continuation."""
    if _result_type(result) == "input_required":
        return False
    if isinstance(result, dict):
        return result.get("isError") is True
    return (
        getattr(result, "is_error", None) is True
        or getattr(result, "isError", None) is True
    )


async def _on_tool_call(data: MCPAnalyticsData, ctx: Any, call_next: Any) -> Any:
    params = ctx.params if isinstance(ctx.params, dict) else {}
    # The tools/call wire always carries a string name; default defensively so a
    # malformed request captures an event rather than raising into dispatch.
    raw_name = params.get("name")
    name: str = raw_name if isinstance(raw_name, str) else ""
    arguments = params.get("arguments") or {}
    request = build_tool_call_request(name, arguments)

    (
        session_id,
        session_id_source,
        client_name,
        client_version,
        protocol_version,
    ) = await _prepare(data, ctx, request)

    start = time.monotonic()
    try:
        result = await call_next(ctx)
    except Exception as error:
        # The middleware chain surfaces a request-side failure as a raised
        # MCPError; capture it before re-raising so the failed call isn't dropped.
        await record_tool_call(
            data,
            session_id,
            name=name,
            arguments=arguments,
            error=error,
            duration_ms=(time.monotonic() - start) * 1000,
            client_name=client_name,
            client_version=client_version,
            protocol_version=protocol_version,
            session_id_source=session_id_source,
        )
        raise
    duration_ms = (time.monotonic() - start) * 1000

    await record_tool_call(
        data,
        session_id,
        name=name,
        arguments=arguments,
        result=_ForceErrorFlag(result) if _is_error_result(result) else result,
        duration_ms=duration_ms,
        client_name=client_name,
        client_version=client_version,
        protocol_version=protocol_version,
        result_type=_result_type(result),
        session_id_source=session_id_source,
    )
    return result


class _ForceErrorFlag:
    """Adapts a v2 wire result so ``record_tool_call``'s ``isError`` detection
    (which reads ``.isError``/``["isError"]``) fires for the value we already
    classified as a tool error — including a model-shaped result whose flag is
    ``is_error``. Wraps, never mutates, the result the tool actually returned."""

    isError = True

    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped

    def __getattr__(self, item: str) -> Any:
        return getattr(self._wrapped, item)


async def _on_tools_list(data: MCPAnalyticsData, ctx: Any, call_next: Any) -> Any:
    request = {"method": "tools/list", "params": {}}
    (
        session_id,
        session_id_source,
        client_name,
        client_version,
        protocol_version,
    ) = await _prepare(data, ctx, request)

    start = time.monotonic()
    try:
        result = await call_next(ctx)
    except Exception as error:
        await record_tools_list(
            data,
            session_id,
            names=[],
            request=request,
            duration_ms=(time.monotonic() - start) * 1000,
            is_error=True,
            error=error,
            client_name=client_name,
            client_version=client_version,
            protocol_version=protocol_version,
            session_id_source=session_id_source,
        )
        raise
    duration_ms = (time.monotonic() - start) * 1000

    names = _listed_tool_names(result)
    empty = len(names) == 0
    await record_tools_list(
        data,
        session_id,
        names=names,
        request=request,
        response=result if isinstance(result, dict) else None,
        duration_ms=duration_ms,
        is_error=empty,
        error="tools/list returned no tools" if empty else None,
        client_name=client_name,
        client_version=client_version,
        protocol_version=protocol_version,
        session_id_source=session_id_source,
    )
    return result


def _listed_tool_names(result: Any) -> list:
    """Tool names out of a v2 ``tools/list`` result (a wire dict ``{"tools":
    [{"name": ...}, ...]}``, or a model with a ``.tools`` list). Read-only — the
    v2 adapter never mutates the response."""
    tools: Any = None
    if isinstance(result, dict):
        tools = result.get("tools")
    else:
        tools = getattr(result, "tools", None)
    if not isinstance(tools, list):
        return []
    names = []
    for tool in tools:
        name = (
            tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)
        )
        if isinstance(name, str):
            names.append(name)
    return names
