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
        protocol_version = _protocol_version(context)
        mcp_session_id = _mcp_session_id(context)
        token, client_name, client_version, protocol_version = (
            resolve_session_and_client(
                mcp_session_id, client_name, client_version, protocol_version
            )
        )
        # Context lookup and dispatch stay adapter-specific; the lifecycle owns
        # only the common session/capture policy.
        lifecycle = start_tool_call_lifecycle(
            data,
            name=name,
            arguments=arguments,
            mcp_session_id=mcp_session_id,
            token=token,
            client_name=client_name,
            client_version=client_version,
            protocol_version=protocol_version,
            extra={
                "session_id": mcp_session_id,
                "ctx": _tool_call_request_context(context),
            },
        )

        if lifecycle.is_missing_capability:
            await lifecycle.record_missing_capability()
            return [
                mcp_types.TextContent(type="text", text=get_more_tools_result_text())
            ]

        # Strip each injected key independently. A tool can declare its own
        # `context` (kept) while `conversation_id` is still SDK-injected (stripped),
        # so coupling both to context-ownership leaked conversation_id into the tool.
        call_arguments = arguments
        if isinstance(arguments, dict):
            strip_keys = set()
            if not _tool_owns_param(server, name, "context"):
                strip_keys.add("context")
            if data.options.enable_conversation_id and not _tool_owns_param(
                server, name, "conversation_id"
            ):
                strip_keys.add("conversation_id")
            if strip_keys:
                call_arguments = {
                    k: v for k, v in arguments.items() if k not in strip_keys
                }

        # Settle the shared session before the tool body runs, so an in-tool
        # `analytics.capture()` is attributed to this caller and not the last one.
        await lifecycle.prime_session()

        start = time.monotonic()
        try:
            result = await original(
                name, call_arguments, context=context, convert_result=convert_result
            )
        except Exception as error:
            # The minted prompt-back was never delivered to the agent — don't stamp
            # an orphan conversation_id it can't echo (an agent-supplied id is kept).
            await lifecycle.record_error(error, (time.monotonic() - start) * 1000)
            raise

        # Deliver the handle first, then capture the result the agent actually got.
        # Two channels: mirrored into structuredContent on every response (for
        # tools whose output schema we declared the key on — clients that read
        # structuredContent never see the text block), and the prompt-back text
        # block on the minting response only.
        delivered = False
        if lifecycle.conversation_id:
            if data.tool_output_instructions.get(name):
                result, delivered = mirror_instructions_into_structured_content(
                    result, lifecycle.conversation_id
                )
            if lifecycle.minted_conversation_id:
                injected = _inject_prompt_back(result, lifecycle.conversation_id)
                if injected is not result:
                    delivered = True
                result = injected

        await lifecycle.record_result(
            result,
            (time.monotonic() - start) * 1000,
            conversation_id_delivered=delivered,
        )
        return result

    setattr(wrapped, _WRAPPED_FLAG, True)
    tool_manager.call_tool = wrapped


# --- tools/list seam ---------------------------------------------------------


def _inject_tool_schemas(server: Any, data: MCPAnalyticsData, tools: list) -> None:
    """Advertise the analytics parameters on a listing's tools, in place.

    Runs on both the client-facing listing and the SDK's internal cache-
    population pass, so the schema the SDK validates against always matches the
    one we advertised — see the note in ``list_handler``.
    """
    for tool in tools:
        mutate_tool_schema(
            data,
            tool,
            schema_attribute="inputSchema",
            owns_context=_tool_owns_context(server, tool.name),
            context_required=True,
        )


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
        # cache. Skip analytics on that internal pass — but still inject the
        # schemas. That cache is the surface the SDK validates calls against
        # (`jsonschema.validate(arguments, inputSchema)` and
        # `(structuredContent, outputSchema)`), and it is rebuilt from scratch
        # whenever an unlisted tool name is called. If it lacks the keys we
        # advertise and write, the SDK rejects the customer's own tool result.
        if req is None:
            result = await original(req)
            _inject_tool_schemas(server, data, extract_tools(result))
            return result

        client_name, client_version = _low_level_client_info(server)
        protocol_version = _low_level_protocol_version(server)
        mcp_session_id = _low_level_session_id(server)
        token, client_name, client_version, protocol_version = (
            resolve_session_and_client(
                mcp_session_id, client_name, client_version, protocol_version
            )
        )
        request = request_to_dict(req)
        extra: Dict[str, Any] = {
            "session_id": mcp_session_id,
            "ctx": _low_level_request_context(server),
        }
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
        # Empty is computed before adding the virtual missing-capability tool.
        names, empty = collect_listed_tools(data, tools)

        _inject_tool_schemas(server, data, tools)

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

    setattr(list_handler, _WRAPPED_FLAG, True)
    handlers[mcp_types.ListToolsRequest] = list_handler


# --- helpers -----------------------------------------------------------------


def _inject_prompt_back(result: Any, conversation_id: str) -> Any:
    """Append the conversation_id prompt-back to a tool result so the agent echoes
    it on later calls. Handles every shape ToolManager.call_tool can return:
    a ``(content_list, structured)`` tuple (the convert_result=True production path),
    a bare content list, or a ``{content: [...]}`` dict — errored dicts included on
    purpose (a first-call failure is exactly when the agent needs the handle).
    Returns the result unchanged (so the caller can detect non-delivery) for
    shapes we can't append to."""
    block = mcp_types.TextContent(
        type="text", text=build_prompt_back(conversation_id)["text"]
    )
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], list):
        return ([*result[0], block], result[1])
    if isinstance(result, list):
        return [*result, block]
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        return {**result, "content": [*result["content"], block]}
    return result


def _tool_owns_param(server: Any, name: str, param: str) -> bool:
    """True when the tool's own function declares ``param`` — then it's a real tool
    argument we must neither inject nor strip (the agent's value belongs to the tool)."""
    tool_manager = getattr(server, "_tool_manager", None)
    if tool_manager is None:
        return False
    tool = tool_manager.get_tool(name)
    fn = getattr(tool, "fn", None)
    if fn is None:
        return False
    try:
        return param in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _tool_owns_context(server: Any, name: str) -> bool:
    return _tool_owns_param(server, name, "context")


def _tool_call_request_context(context: Any) -> Any:
    """The request context behind a FastMCP ``Context``, or ``None``.

    ``Context.request_context`` is a property that *raises* ``ValueError`` when
    the call happens outside a request — which the public ``FastMCP.call_tool()``
    entry point does. A bare ``getattr(..., None)`` only swallows
    ``AttributeError``, so it would let that escape into the customer's tool call.
    """
    try:
        return context.request_context
    except (LookupError, ValueError, AttributeError):
        return None


def _low_level_request_context(server: Any) -> Any:
    """The underlying low-level server's request_context, set during a request. The
    tools/list handler runs on ``server._mcp_server``, so client info / session id
    come from there rather than from a FastMCP ``Context`` (which only the call path has)."""
    low_level = getattr(server, "_mcp_server", None)
    if low_level is None:
        return None
    try:
        return low_level.request_context
    except (LookupError, AttributeError):
        return None


def _low_level_client_info(server: Any) -> Tuple[Optional[str], Optional[str]]:
    ctx = _low_level_request_context(server)
    try:
        client_params = ctx.session.client_params
        if client_params and client_params.clientInfo:
            return client_params.clientInfo.name, client_params.clientInfo.version
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _low_level_session_id(server: Any) -> Optional[str]:
    ctx = _low_level_request_context(server)
    try:
        request = getattr(ctx, "request", None)
        headers = getattr(request, "headers", None)
        if headers is not None:
            return headers.get("mcp-session-id")
    except Exception:  # noqa: BLE001
        pass
    return None


def _low_level_protocol_version(server: Any) -> Optional[str]:
    ctx = _low_level_request_context(server)
    try:
        client_params = ctx.session.client_params
        if client_params:
            return client_params.protocolVersion
    except Exception:  # noqa: BLE001
        pass
    return None


def _client_info(context: Any) -> Tuple[Optional[str], Optional[str]]:
    try:
        client_params = context.request_context.session.client_params
        if client_params and client_params.clientInfo:
            return client_params.clientInfo.name, client_params.clientInfo.version
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _protocol_version(context: Any) -> Optional[str]:
    try:
        client_params = context.request_context.session.client_params
        if client_params:
            return client_params.protocolVersion
    except Exception:  # noqa: BLE001
        pass
    return None


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
