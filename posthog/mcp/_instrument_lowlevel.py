# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Low-level ``mcp.server.Server`` adapter.

The low-level server keeps its handlers in a public ``request_handlers`` dict, so
we wrap the ``CallToolRequest`` and ``ListToolsRequest`` entries directly. Unlike
FastMCP, the low-level ``call_tool`` handler catches exceptions and returns a
``CallToolResult`` with ``isError=True`` rather than raising — so we detect errors
from the result, not a ``try/except``. Session and client info are read from the
server's ``request_context`` contextvar (the handler receives only the request).
"""

from __future__ import annotations

import inspect
import time
from typing import Any, Optional, Tuple

import mcp.types as mcp_types

from ._context_parameters import schema_has_param
from ._conversation_id import build_prompt_back
from ._instrumentation import (
    _to_jsonable,
    append_get_more_tools,
    collect_listed_tools,
    extract_tools,
    mutate_tool_schema,
    request_to_dict,
    resolve_session_and_client,
    start_tool_call_lifecycle,
    start_tools_list_lifecycle,
)
from ._internal import MCPAnalyticsData
from ._output_instructions import mirror_instructions_into_structured_content
from .logger import log
from .tools import get_more_tools_result_text, resolve_missing_capability_tool_name

_WRAPPED_FLAG = "__posthog_mcp_wrapped__"


def instrument_low_level(server: Any, data: MCPAnalyticsData) -> None:
    """Instrument a raw ``mcp.server.Server``. ``context`` is injected as an
    optional schema property and NOT stripped — that schema is also the call's
    validation schema, and a typical ``(name, arguments)`` handler ignores extra keys."""
    data.server_name = getattr(server, "name", None)
    data.server_version = getattr(server, "version", None)
    _wrap_call_tool(server, data, strip_injected=False)
    _wrap_list_tools(server, data, context_required=False)


def instrument_fastmcp_v2(server: Any, data: MCPAnalyticsData) -> None:
    """Instrument jlowin's standalone ``fastmcp.FastMCP`` (FastMCP 2.0). It exposes a
    ``_mcp_server`` (a subclass of the official low-level Server) with the same
    ``request_handlers`` seam, but validates tool args against the function
    signature and rejects unexpected kwargs — so we STRIP the injected
    ``context``/``conversation_id`` before dispatch (like the official FastMCP path)."""
    low_level = getattr(server, "_mcp_server", None)
    if low_level is None:
        log("Warning: fastmcp.FastMCP has no _mcp_server; cannot instrument.")
        return
    data.server_name = getattr(server, "name", None) or getattr(low_level, "name", None)
    data.server_version = getattr(server, "version", None) or getattr(
        low_level, "version", None
    )
    _wrap_call_tool(low_level, data, strip_injected=True, high_level=server)
    # `context` is advertised but NOT marked required here. This adapter strips
    # the injected parameters before the SDK's own input validation runs, so a
    # schema that requires `context` contradicts the arguments the SDK actually
    # sees: under `FastMCP(strict_input_validation=True)` every call fails with
    # "'context' is a required property".
    _wrap_list_tools(low_level, data, context_required=False)


def _wrap_call_tool(
    server: Any, data: MCPAnalyticsData, *, strip_injected: bool, high_level: Any = None
) -> None:
    handlers = server.request_handlers
    original = handlers.get(mcp_types.CallToolRequest)
    if original is None or getattr(original, _WRAPPED_FLAG, False):
        return

    async def handler(req: Any) -> Any:
        name = req.params.name
        arguments = dict(req.params.arguments or {})
        client_name, client_version = _client_info(server)
        protocol_version = _protocol_version(server)
        mcp_session_id = _mcp_session_id(server)
        token, client_name, client_version, protocol_version = (
            resolve_session_and_client(
                mcp_session_id, client_name, client_version, protocol_version
            )
        )
        lifecycle = start_tool_call_lifecycle(
            data,
            name=name,
            arguments=arguments,
            mcp_session_id=mcp_session_id,
            token=token,
            client_name=client_name,
            client_version=client_version,
            protocol_version=protocol_version,
            extra={"session_id": mcp_session_id, "ctx": _request_context(server)},
        )

        if lifecycle.is_missing_capability:
            await lifecycle.record_missing_capability()
            return mcp_types.ServerResult(
                mcp_types.CallToolResult(
                    content=[
                        mcp_types.TextContent(
                            type="text", text=get_more_tools_result_text()
                        )
                    ],
                    isError=False,
                )
            )

        # On raw low-level servers `context`/`conversation_id` are injected as
        # *optional* schema properties and left in place (a (name, arguments)
        # handler ignores extra keys). FastMCP 2.0 validates against the function
        # signature and rejects unexpected kwargs, so strip them before dispatch —
        # but NOT a key the tool declares itself (that's a real argument). Ownership
        # is read from the tool's own signature, so it holds with or without a prior
        # tools/list and across stateless per-request server instances.
        if strip_injected and req.params.arguments:
            owned = await _tool_owned_injected_keys(high_level, name)
            for key in ("context", "conversation_id"):
                if key not in owned:
                    req.params.arguments.pop(key, None)

        # Settle the shared session before the tool body runs, so an in-tool
        # `analytics.capture()` is attributed to this caller and not the last one.
        await lifecycle.prime_session()

        start = time.monotonic()
        try:
            result = await original(req)
        except Exception as error:
            # The @server.call_tool() decorator converts raises into
            # CallToolResult(isError=True), but a handler wired straight into
            # request_handlers can raise — capture before re-raising so the failed
            # call isn't silently dropped. A minted (undelivered) conversation_id is
            # not stamped, matching the FastMCP path.
            await lifecycle.record_error(error, (time.monotonic() - start) * 1000)
            raise
        duration_ms = (time.monotonic() - start) * 1000

        # The low-level handler already converted any exception to a
        # CallToolResult(isError=True); record_tool_call detects that from the result.
        call_result = getattr(result, "root", result)

        # Deliver the handle before capture, over both channels a result has:
        # mirrored into structuredContent on every response (for tools whose
        # output schema we declared the key on — clients that read
        # structuredContent never see the text block), and the prompt-back text
        # block on the minting response only. Only stamp a minted conversation_id
        # when it actually reached the agent, so we don't record an orphan id.
        # Errored results carry it on purpose: a first-call failure is exactly
        # when the agent needs the handle, or the retry starts a fresh conversation.
        delivered = False
        if lifecycle.conversation_id:
            if data.tool_output_instructions.get(name):
                call_result, delivered = mirror_instructions_into_structured_content(
                    call_result, lifecycle.conversation_id
                )
            if lifecycle.minted_conversation_id:
                content = getattr(call_result, "content", None)
                if isinstance(content, list):
                    block = mcp_types.TextContent(
                        type="text",
                        text=build_prompt_back(lifecycle.conversation_id)["text"],
                    )
                    # Copy rather than append in place — a shared or cached result
                    # object would accumulate a block per conversation and leak
                    # earlier callers' handles to later ones.
                    copy_model = getattr(call_result, "model_copy", None)
                    if callable(copy_model):
                        call_result = copy_model(update={"content": [*content, block]})
                    else:
                        content.append(block)
                    delivered = True
            # Hand back whatever copy we made, rewrapped as the SDK expects.
            if call_result is not getattr(result, "root", result):
                result = (
                    mcp_types.ServerResult(call_result)
                    if hasattr(result, "root")
                    else call_result
                )

        await lifecycle.record_result(
            call_result,
            duration_ms,
            conversation_id_delivered=delivered,
        )
        return result

    setattr(handler, _WRAPPED_FLAG, True)
    handlers[mcp_types.CallToolRequest] = handler


def _inject_tool_schemas(
    data: MCPAnalyticsData, tools: list, *, context_required: bool
) -> None:
    """Advertise the analytics parameters on a listing's tools, in place.

    Runs on both the client-facing listing and the SDK's internal cache-
    population pass, so the schema the SDK validates against always matches the
    one we advertised — see the note in ``handler``.
    """
    for tool in tools:
        schema = getattr(tool, "inputSchema", None)
        mutate_tool_schema(
            data,
            tool,
            schema_attribute="inputSchema",
            owns_context=schema_has_param(schema, "context"),
            context_required=context_required,
        )


def _wrap_list_tools(
    server: Any, data: MCPAnalyticsData, *, context_required: bool
) -> None:
    handlers = server.request_handlers
    original = handlers.get(mcp_types.ListToolsRequest)
    if original is None or getattr(original, _WRAPPED_FLAG, False):
        return

    async def handler(req: Any) -> Any:
        # The server calls the handler with None to populate its tool cache.
        # Skip analytics there — but still inject, because that cache is the
        # schema the SDK validates calls against. This adapter advertises
        # `context`/`conversation_id` without stripping them, so a cache built
        # from un-injected schemas rejects the very arguments we told the agent
        # to send ("Additional properties are not allowed") on any tool with
        # `additionalProperties: false`.
        if req is None:
            result = await original(req)
            _inject_tool_schemas(
                data, extract_tools(result), context_required=context_required
            )
            return result

        client_name, client_version = _client_info(server)
        protocol_version = _protocol_version(server)
        mcp_session_id = _mcp_session_id(server)
        token, client_name, client_version, protocol_version = (
            resolve_session_and_client(
                mcp_session_id, client_name, client_version, protocol_version
            )
        )
        request = request_to_dict(req)
        # `ctx` is the SDK's own per-request context, handed to host callbacks
        # unchanged and identically on both SDK majors (read headers off it with
        # the exported `get_request_headers`). Never captured — the event
        # pipeline keeps only a scalar projection of `extra`.
        extra = {"session_id": mcp_session_id, "ctx": _request_context(server)}
        # Resolve session, emit $mcp_initialize (once per session) and identify here
        # too — a client may list tools without ever calling one.
        lifecycle = await start_tools_list_lifecycle(
            data,
            request=request,
            extra=extra,
            mcp_session_id=mcp_session_id,
            token=token,
            client_name=client_name,
            client_version=client_version,
            protocol_version=protocol_version,
        )

        start = time.monotonic()
        try:
            result = await original(req)
        except Exception as error:
            await lifecycle.record_error(error, (time.monotonic() - start) * 1000)
            raise
        duration_ms = (time.monotonic() - start) * 1000
        tools = extract_tools(result)

        # Zero advertised tools is treated as an errored tools/list before the
        # virtual missing-capability tool is appended.
        names, empty = collect_listed_tools(data, tools)

        _inject_tool_schemas(data, tools, context_required=context_required)

        if data.options.report_missing:
            missing_name = resolve_missing_capability_tool_name(data.options)
            if not any(t.name == missing_name for t in tools):
                append_get_more_tools(result, missing_name)
                names.append(missing_name)

        await lifecycle.record_result(
            names=names,
            response=_to_jsonable(result),
            duration_ms=duration_ms,
            is_empty=empty,
        )

        return result

    setattr(handler, _WRAPPED_FLAG, True)
    handlers[mcp_types.ListToolsRequest] = handler


async def _tool_owned_injected_keys(high_level: Any, name: str) -> set:
    """Which of (``context``, ``conversation_id``) the jlowin FastMCP tool declares
    itself, read from its function signature. These are real tool arguments we must
    not strip. On any lookup failure, return empty (strip both) — same as the prior
    unconditional behaviour, so a flaky introspection never leaks an injected key."""
    if high_level is None:
        return set()
    try:
        tool = await high_level.get_tool(name)
        fn = getattr(tool, "fn", None)
        params = set(inspect.signature(fn).parameters) if fn is not None else set()
        return {k for k in ("context", "conversation_id") if k in params}
    except Exception:  # noqa: BLE001 - introspection is best-effort
        return set()


def _request_context(server: Any) -> Any:
    try:
        return server.request_context
    except (LookupError, AttributeError):
        return None


def _client_info(server: Any) -> Tuple[Optional[str], Optional[str]]:
    ctx = _request_context(server)
    try:
        client_params = ctx.session.client_params
        if client_params and client_params.clientInfo:
            return client_params.clientInfo.name, client_params.clientInfo.version
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _protocol_version(server: Any) -> Optional[str]:
    ctx = _request_context(server)
    try:
        client_params = ctx.session.client_params
        if client_params:
            return client_params.protocolVersion
    except Exception:  # noqa: BLE001
        pass
    return None


def _mcp_session_id(server: Any) -> Optional[str]:
    ctx = _request_context(server)
    try:
        request = getattr(ctx, "request", None)
        headers = getattr(request, "headers", None)
        if headers is not None:
            return headers.get("mcp-session-id")
    except Exception:  # noqa: BLE001
        pass
    return None
