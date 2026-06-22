# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""FastMCP adapter.

Rather than wrap each tool individually (as the TS high-level adapter does with a
Proxy), we wrap two *central* seams the ``mcp`` SDK routes everything through:

* ``ToolManager.call_tool`` — every tool call dispatches here. We strip the
  injected ``context`` before Pydantic validation, time the call, capture the
  result/exception, and re-raise. Late-registered tools are covered automatically.
* the low-level ``ListToolsRequest`` handler — every ``tools/list`` response is
  built here. We capture ``$mcp_tools_list`` and inject the ``context`` parameter
  into each advertised tool schema.

``$mcp_initialize`` is emitted lazily on the first tool call (the Python SDK
handles ``initialize`` in the session layer, not via ``request_handlers``).
"""

from __future__ import annotations

import inspect
import time
from typing import Any, Dict, Optional, Tuple

import mcp.types as mcp_types

from .context_parameters import (
    add_context_parameter_to_schema,
    get_context_description,
    is_context_enabled,
)
from .conversation_id import (
    add_conversation_id_to_schema,
    build_prompt_back,
    resolve_conversation_id,
)
from .instrumentation import (
    append_get_more_tools,
    build_tool_call_request,
    extract_tools,
    prepare_request,
    read_tool_category,
    record_missing_capability,
    record_tool_call,
    record_tools_list,
    request_to_dict,
)
from .internal import MCPAnalyticsData
from .logger import log
from .session import resolve_session_id
from .tools import (
    GET_MORE_TOOLS_NAME as _GET_MORE_TOOLS_NAME,
    get_more_tools_result_text,
    resolve_missing_capability_tool_name,
)

_INJECTED_KEYS = ("context", "conversation_id")
_WRAPPED_FLAG = "__posthog_mcp_wrapped__"


def instrument_fastmcp(server: Any, data: MCPAnalyticsData) -> None:
    data.server_name = getattr(server, "name", None) or getattr(
        getattr(server, "_mcp_server", None), "name", None
    )
    data.server_version = getattr(getattr(server, "_mcp_server", None), "version", None)
    _wrap_tool_manager_call(server, data)
    _wrap_list_tools_handler(server, data)


# --- tool call seam ----------------------------------------------------------


def _wrap_tool_manager_call(server: Any, data: MCPAnalyticsData) -> None:
    tool_manager = getattr(server, "_tool_manager", None)
    if tool_manager is None:
        log(
            "Warning: FastMCP server has no _tool_manager; tool calls will not be captured."
        )
        return

    original = tool_manager.call_tool
    if getattr(original, _WRAPPED_FLAG, False):
        return

    async def wrapped(
        name: str,
        arguments: Dict[str, Any],
        context: Any = None,
        convert_result: bool = False,
    ) -> Any:
        client_name, client_version = _client_info(context)
        mcp_session_id = _mcp_session_id(context)
        request = build_tool_call_request(name, arguments)
        extra: Dict[str, Any] = {"session_id": mcp_session_id}

        session_id = await prepare_request(
            data,
            mcp_session_id=mcp_session_id,
            client_name=client_name,
            client_version=client_version,
            request=request,
            extra=extra,
        )

        missing_name = resolve_missing_capability_tool_name(data.options)
        if data.options.report_missing and name == missing_name:
            await record_missing_capability(
                data,
                session_id,
                tool_name=missing_name,
                context=(arguments or {}).get("context"),
                arguments=arguments,
                client_name=client_name,
                client_version=client_version,
                extra=extra,
            )
            return [
                mcp_types.TextContent(type="text", text=get_more_tools_result_text())
            ]

        conversation_id, minted = resolve_conversation_id(
            data.options.enable_conversation_id, arguments, name, missing_name
        )

        call_arguments = arguments
        if isinstance(arguments, dict) and not _tool_owns_context(server, name):
            call_arguments = {
                k: v for k, v in arguments.items() if k not in _INJECTED_KEYS
            }

        start = time.monotonic()
        try:
            result = await original(
                name, call_arguments, context=context, convert_result=convert_result
            )
        except Exception as error:
            # The minted prompt-back was never delivered to the agent — don't stamp
            # an orphan conversation_id it can't echo (an agent-supplied id is kept).
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

        # Inject the prompt-back first, then capture the delivered result. Only stamp
        # a minted conversation_id when it was actually appended to what the agent got.
        delivered_conversation_id = conversation_id
        if minted and conversation_id:
            injected = _inject_prompt_back(result, conversation_id)
            if injected is result:
                delivered_conversation_id = (
                    None  # not injectable (e.g. tuple/scalar result)
                )
            result = injected

        await record_tool_call(
            data,
            session_id,
            name=name,
            arguments=arguments,
            result=result,
            duration_ms=(time.monotonic() - start) * 1000,
            client_name=client_name,
            client_version=client_version,
            conversation_id=delivered_conversation_id,
            extra=extra,
        )
        return result

    setattr(wrapped, _WRAPPED_FLAG, True)
    tool_manager.call_tool = wrapped


# --- tools/list seam ---------------------------------------------------------


def _wrap_list_tools_handler(server: Any, data: MCPAnalyticsData) -> None:
    low_level = getattr(server, "_mcp_server", None)
    if low_level is None:
        return
    handlers = low_level.request_handlers
    original = handlers.get(mcp_types.ListToolsRequest)
    if original is None or getattr(original, _WRAPPED_FLAG, False):
        return

    async def list_handler(req: Any) -> Any:
        # The low-level server calls the handler with None to populate its tool
        # cache; don't capture or inject on that internal pass.
        if req is None:
            return await original(req)

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

        context_enabled = is_context_enabled(data.options.context)
        description = get_context_description(data.options.context)
        for tool in tools:
            if tool.name == _GET_MORE_TOOLS_NAME:
                continue
            owns_context = _tool_owns_context(server, tool.name)
            schema = getattr(tool, "inputSchema", None)
            if context_enabled and not owns_context:
                schema = add_context_parameter_to_schema(schema, tool.name, description)
            if data.options.enable_conversation_id:
                schema = add_conversation_id_to_schema(schema, tool.name)
            if schema is not getattr(tool, "inputSchema", None):
                try:
                    tool.inputSchema = schema
                except Exception:  # noqa: BLE001 - some schema attrs may be read-only
                    log(f"WARN: could not set inputSchema on tool {tool.name}")

        if data.options.report_missing:
            missing_name = resolve_missing_capability_tool_name(data.options)
            if not any(t.name == missing_name for t in tools):
                append_get_more_tools(result, missing_name)
                names.append(missing_name)

        session_id = await resolve_session_id(data, None)
        await record_tools_list(
            data, session_id, names=names, request=request_to_dict(req)
        )

        return result

    setattr(list_handler, _WRAPPED_FLAG, True)
    handlers[mcp_types.ListToolsRequest] = list_handler


# --- helpers -----------------------------------------------------------------


def _inject_prompt_back(result: Any, conversation_id: str) -> Any:
    """Append the conversation_id prompt-back to a tool result so the agent echoes
    it on later calls. Handles every shape ToolManager.call_tool can return:
    a ``(content_list, structured)`` tuple (the convert_result=True production path),
    a bare content list, or a ``{content: [...]}`` dict. Returns the result unchanged
    (so the caller can detect non-delivery) for shapes we can't append to."""
    block = mcp_types.TextContent(
        type="text", text=build_prompt_back(conversation_id)["text"]
    )
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], list):
        return ([*result[0], block], result[1])
    if isinstance(result, list):
        return [*result, block]
    if (
        isinstance(result, dict)
        and isinstance(result.get("content"), list)
        and not result.get("isError")
    ):
        return {**result, "content": [*result["content"], block]}
    return result


def _tool_owns_context(server: Any, name: str) -> bool:
    """True when the tool's own function declares a ``context`` parameter — then we
    must not inject or strip our analytics ``context``."""
    tool_manager = getattr(server, "_tool_manager", None)
    if tool_manager is None:
        return False
    tool = tool_manager.get_tool(name)
    fn = getattr(tool, "fn", None)
    if fn is None:
        return False
    try:
        return "context" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _client_info(context: Any) -> Tuple[Optional[str], Optional[str]]:
    try:
        client_params = context.request_context.session.client_params
        if client_params and client_params.clientInfo:
            return client_params.clientInfo.name, client_params.clientInfo.version
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _mcp_session_id(context: Any) -> Optional[str]:
    """Best-effort transport session id (e.g. the ``Mcp-Session-Id`` header on the
    streamable-HTTP transport). Returns ``None`` for stdio, where the SDK-generated
    session is used instead."""
    try:
        request = getattr(context.request_context, "request", None)
        headers = getattr(request, "headers", None)
        if headers is not None:
            return headers.get("mcp-session-id")
    except Exception:  # noqa: BLE001
        pass
    return None
