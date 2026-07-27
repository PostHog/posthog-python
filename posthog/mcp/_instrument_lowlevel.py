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

from ._context_parameters import (
    add_context_parameter_to_schema,
    get_context_description,
    is_context_enabled,
)
from ._conversation_id import (
    add_conversation_id_to_schema,
    build_prompt_back,
    resolve_conversation_id,
)
from ._instrumentation import (
    _to_jsonable,
    append_get_more_tools,
    build_tool_call_request,
    extract_tools,
    prepare_request,
    read_tool_category,
    record_missing_capability,
    record_tool_call,
    record_tools_list,
    request_to_dict,
    resolve_session_and_client,
)
from ._internal import MCPAnalyticsData
from .logger import log
from .tools import (
    GET_MORE_TOOLS_NAME as _GET_MORE_TOOLS_NAME,
    get_more_tools_result_text,
    resolve_missing_capability_tool_name,
)

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
    _wrap_list_tools(low_level, data, context_required=True)


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
        mcp_session_id = _mcp_session_id(server)
        token, client_name, client_version = resolve_session_and_client(
            mcp_session_id, client_name, client_version
        )
        request = build_tool_call_request(name, arguments)
        extra = {"session_id": mcp_session_id}

        session_id = await prepare_request(
            data,
            mcp_session_id=mcp_session_id,
            client_name=client_name,
            client_version=client_version,
            request=request,
            extra=extra,
            token=token,
        )

        missing_name = resolve_missing_capability_tool_name(data.options)
        if data.options.report_missing and name == missing_name:
            await record_missing_capability(
                data,
                session_id,
                tool_name=missing_name,
                context=arguments.get("context"),
                arguments=arguments,
                client_name=client_name,
                client_version=client_version,
                extra=extra,
            )
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

        conversation_id, minted = resolve_conversation_id(
            data.options.enable_conversation_id, arguments, name, missing_name
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

        start = time.monotonic()
        try:
            result = await original(req)
        except Exception as error:
            # The @server.call_tool() decorator converts raises into
            # CallToolResult(isError=True), but a handler wired straight into
            # request_handlers can raise — capture before re-raising so the failed
            # call isn't silently dropped. A minted (undelivered) conversation_id is
            # not stamped, matching the FastMCP path.
            await record_tool_call(
                data,
                session_id,
                name=name,
                arguments=arguments,
                error=error,
                duration_ms=(time.monotonic() - start) * 1000,
                client_name=client_name,
                client_version=client_version,
                conversation_id=None if minted else conversation_id,
                extra=extra,
            )
            raise
        duration_ms = (time.monotonic() - start) * 1000

        # The low-level handler already converted any exception to a
        # CallToolResult(isError=True); record_tool_call detects that from the result.
        call_result = getattr(result, "root", result)

        # Inject the prompt-back before capture; only stamp a minted conversation_id
        # when it was actually delivered (not on isError / non-list results), so we
        # don't record an orphan id the agent never received.
        delivered_conversation_id = conversation_id
        if minted and conversation_id:
            content = getattr(call_result, "content", None)
            if not getattr(call_result, "isError", False) and isinstance(content, list):
                content.append(
                    mcp_types.TextContent(
                        type="text", text=build_prompt_back(conversation_id)["text"]
                    )
                )
            else:
                delivered_conversation_id = None

        await record_tool_call(
            data,
            session_id,
            name=name,
            arguments=arguments,
            result=call_result,
            duration_ms=duration_ms,
            client_name=client_name,
            client_version=client_version,
            conversation_id=delivered_conversation_id,
            extra=extra,
        )
        return result

    setattr(handler, _WRAPPED_FLAG, True)
    handlers[mcp_types.CallToolRequest] = handler


def _wrap_list_tools(
    server: Any, data: MCPAnalyticsData, *, context_required: bool
) -> None:
    handlers = server.request_handlers
    original = handlers.get(mcp_types.ListToolsRequest)
    if original is None or getattr(original, _WRAPPED_FLAG, False):
        return

    async def handler(req: Any) -> Any:
        # The server calls the handler with None to populate its tool cache;
        # don't capture or inject on that internal pass.
        if req is None:
            return await original(req)

        client_name, client_version = _client_info(server)
        mcp_session_id = _mcp_session_id(server)
        token, client_name, client_version = resolve_session_and_client(
            mcp_session_id, client_name, client_version
        )
        request = request_to_dict(req)
        extra = {"session_id": mcp_session_id}
        # Resolve session, emit $mcp_initialize (once per session) and identify here
        # too — a client may list tools without ever calling one.
        session_id = await prepare_request(
            data,
            mcp_session_id=mcp_session_id,
            client_name=client_name,
            client_version=client_version,
            request=request,
            extra=extra,
            token=token,
        )

        start = time.monotonic()
        try:
            result = await original(req)
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
                extra=extra,
            )
            raise
        duration_ms = (time.monotonic() - start) * 1000
        tools = extract_tools(result)

        names = []
        for tool in tools:
            names.append(tool.name)
            if getattr(tool, "description", None):
                data.tool_descriptions[tool.name] = tool.description
            category = read_tool_category(tool)
            if category:
                data.tool_categories[tool.name] = category

        # Zero advertised tools is treated as an errored tools/list (parity with the
        # TS SDK) — captured before we append our own get_more_tools virtual tool.
        empty = len(tools) == 0

        context_enabled = is_context_enabled(data.options.context)
        description = get_context_description(data.options.context)
        for tool in tools:
            if tool.name == _GET_MORE_TOOLS_NAME:
                continue
            schema = getattr(tool, "inputSchema", None)
            # required follows the path: raw low-level validates the call against
            # this same schema (optional), FastMCP 2.0 strips it first (required-advisory).
            if context_enabled and not _schema_has_param(schema, "context"):
                schema = add_context_parameter_to_schema(
                    schema, tool.name, description, required=context_required
                )
            if data.options.enable_conversation_id and not _schema_has_param(
                schema, "conversation_id"
            ):
                schema = add_conversation_id_to_schema(schema, tool.name)
            if schema is not getattr(tool, "inputSchema", None):
                try:
                    tool.inputSchema = schema
                except Exception:  # noqa: BLE001
                    log(f"WARN: could not set inputSchema on tool {tool.name}")

        if data.options.report_missing:
            missing_name = resolve_missing_capability_tool_name(data.options)
            if not any(t.name == missing_name for t in tools):
                append_get_more_tools(result, missing_name)
                names.append(missing_name)

        await record_tools_list(
            data,
            session_id,
            names=names,
            request=request,
            response=_to_jsonable(result),
            duration_ms=duration_ms,
            is_error=empty,
            error="tools/list returned no tools" if empty else None,
            client_name=client_name,
            client_version=client_version,
            extra=extra,
        )

        return result

    setattr(handler, _WRAPPED_FLAG, True)
    handlers[mcp_types.ListToolsRequest] = handler


def _schema_has_param(schema: Any, name: str) -> bool:
    return (
        isinstance(schema, dict)
        and isinstance(schema.get("properties"), dict)
        and name in schema["properties"]
    )


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
