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

import time
from typing import Any, Optional, Tuple

import mcp.types as mcp_types

from .context_parameters import (
    add_context_parameter_to_schema,
    get_context_description,
    is_context_enabled,
)
from .instrumentation import (
    build_tool_call_request,
    extract_tools,
    prepare_request,
    read_tool_category,
    record_tool_call,
    record_tools_list,
    request_to_dict,
)
from .internal import MCPAnalyticsData
from .logger import log
from .session import resolve_session_id

_GET_MORE_TOOLS_NAME = "get_more_tools"
_WRAPPED_FLAG = "__posthog_mcp_wrapped__"


def instrument_low_level(server: Any, data: MCPAnalyticsData) -> None:
    data.server_name = getattr(server, "name", None)
    data.server_version = getattr(server, "version", None)
    _wrap_call_tool(server, data)
    _wrap_list_tools(server, data)


def _wrap_call_tool(server: Any, data: MCPAnalyticsData) -> None:
    handlers = server.request_handlers
    original = handlers.get(mcp_types.CallToolRequest)
    if original is None or getattr(original, _WRAPPED_FLAG, False):
        return

    async def handler(req: Any) -> Any:
        name = req.params.name
        arguments = dict(req.params.arguments or {})
        client_name, client_version = _client_info(server)
        mcp_session_id = _mcp_session_id(server)
        request = build_tool_call_request(name, arguments)
        extra = {"session_id": mcp_session_id}

        session_id = await prepare_request(
            data,
            mcp_session_id=mcp_session_id,
            client_name=client_name,
            client_version=client_version,
            request=request,
            extra=extra,
        )

        # Note: `context` is injected as an *optional* schema property (see
        # _wrap_list_tools), so we do NOT strip it here — the low-level handler
        # validates inbound args against the same advertised schema, and the
        # user's (name, arguments) handler harmlessly ignores the extra key.
        start = time.monotonic()
        result = await original(req)
        duration_ms = (time.monotonic() - start) * 1000

        # The low-level handler already converted any exception to a
        # CallToolResult(isError=True); record_tool_call detects that from the result.
        call_result = getattr(result, "root", result)
        await record_tool_call(
            data,
            session_id,
            name=name,
            arguments=arguments,
            result=call_result,
            duration_ms=duration_ms,
            client_name=client_name,
            client_version=client_version,
            extra=extra,
        )
        return result

    setattr(handler, _WRAPPED_FLAG, True)
    handlers[mcp_types.CallToolRequest] = handler


def _wrap_list_tools(server: Any, data: MCPAnalyticsData) -> None:
    handlers = server.request_handlers
    original = handlers.get(mcp_types.ListToolsRequest)
    if original is None or getattr(original, _WRAPPED_FLAG, False):
        return

    async def handler(req: Any) -> Any:
        result = await original(req)
        tools = extract_tools(result)

        names = []
        for tool in tools:
            names.append(tool.name)
            if getattr(tool, "description", None):
                data.tool_descriptions[tool.name] = tool.description
            category = read_tool_category(tool)
            if category:
                data.tool_categories[tool.name] = category

        client_name, client_version = _client_info(server)
        session_id = await resolve_session_id(data, _mcp_session_id(server))
        record_tools_list(
            data,
            session_id,
            names=names,
            request=request_to_dict(req),
            client_name=client_name,
            client_version=client_version,
        )

        if is_context_enabled(data.options.context):
            description = get_context_description(data.options.context)
            for tool in tools:
                if tool.name == _GET_MORE_TOOLS_NAME:
                    continue
                schema = getattr(tool, "inputSchema", None)
                if _schema_has_context(schema):
                    continue  # tool already declares `context`; leave it alone
                # Optional (required=False): the advertised schema is also the
                # validation schema for low-level calls, so a call without
                # `context` must still pass.
                new_schema = add_context_parameter_to_schema(
                    schema, tool.name, description, required=False
                )
                try:
                    tool.inputSchema = new_schema
                except Exception:  # noqa: BLE001
                    log(f"WARN: could not set inputSchema on tool {tool.name}")

        return result

    setattr(handler, _WRAPPED_FLAG, True)
    handlers[mcp_types.ListToolsRequest] = handler


def _schema_has_context(schema: Any) -> bool:
    return (
        isinstance(schema, dict)
        and isinstance(schema.get("properties"), dict)
        and "context" in schema["properties"]
    )


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
