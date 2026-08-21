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
    prime_session,
    read_tool_category,
    record_missing_capability,
    record_tool_call,
    record_tools_list,
    request_to_dict,
    resolve_session_and_client,
)
from ._internal import MCPAnalyticsData
from ._output_instructions import (
    add_instructions_to_output_schema,
    mirror_instructions_into_structured_content,
)
from .logger import log
from .tools import (
    GET_MORE_TOOLS_NAME as _GET_MORE_TOOLS_NAME,
    get_more_tools_result_text,
    resolve_missing_capability_tool_name,
)

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
        request = build_tool_call_request(name, arguments)
        # `ctx` is the SDK's own per-request context, handed to host callbacks
        # unchanged and identically on both SDK majors (read headers off it with
        # the exported `get_request_headers`). Never captured — the event
        # pipeline keeps only a scalar projection of `extra`.
        extra: Dict[str, Any] = {
            "session_id": mcp_session_id,
            "ctx": _tool_call_request_context(context),
        }

        # Resolve the conversation handle before the session: when the agent
        # carries (or is about to receive) one, it anchors $session_id for every
        # event of this request (ADR-0004) — the only correlation that survives
        # the 2026-07-28 revision's per-request server instances.
        missing_name = resolve_missing_capability_tool_name(data.options)
        conversation_id, minted = resolve_conversation_id(
            data.options.enable_conversation_id, arguments, name, missing_name
        )

        # Resolved once the handle's fate is known — a minted handle only
        # anchors the session after we have confirmed the agent received it,
        # so the call that mints it still joins its own conversation.
        async def _session(anchor: Optional[str]) -> str:
            return await prepare_request(
                data,
                mcp_session_id=mcp_session_id,
                client_name=client_name,
                client_version=client_version,
                protocol_version=protocol_version,
                request=request,
                extra=extra,
                token=token,
                conversation_id=anchor,
            )

        if data.options.report_missing and name == missing_name:
            session_id = await _session(None)
            await record_missing_capability(
                data,
                session_id,
                tool_name=missing_name,
                context=(arguments or {}).get("context"),
                arguments=arguments,
                client_name=client_name,
                client_version=client_version,
                protocol_version=protocol_version,
                extra=extra,
            )
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
        await prime_session(data, mcp_session_id=mcp_session_id, token=token)

        start = time.monotonic()
        try:
            result = await original(
                name, call_arguments, context=context, convert_result=convert_result
            )
        except Exception as error:
            # The minted prompt-back was never delivered to the agent — don't stamp
            # an orphan conversation_id it can't echo (an agent-supplied id is kept).
            session_id = await _session(None if minted else conversation_id)
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
                conversation_id=None if minted else conversation_id,
                extra=extra,
            )
            raise

        # Deliver the handle first, then capture the result the agent actually got.
        # Two channels: mirrored into structuredContent on every response (for
        # tools whose output schema we declared the key on — clients that read
        # structuredContent never see the text block), and the prompt-back text
        # block on the minting response only.
        delivered_conversation_id = conversation_id
        if conversation_id:
            delivered = False
            if data.tool_output_instructions.get(name):
                result, delivered = mirror_instructions_into_structured_content(
                    result, conversation_id
                )
            if minted:
                injected = _inject_prompt_back(result, conversation_id)
                if injected is not result:
                    delivered = True
                result = injected
                # Only a minted handle can be lost — one the agent supplied, it has.
                if not delivered:
                    delivered_conversation_id = None

        session_id = await _session(delivered_conversation_id)
        await record_tool_call(
            data,
            session_id,
            name=name,
            arguments=arguments,
            result=result,
            duration_ms=(time.monotonic() - start) * 1000,
            client_name=client_name,
            client_version=client_version,
            protocol_version=protocol_version,
            conversation_id=delivered_conversation_id,
            extra=extra,
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
        # Declare the structuredContent channel and remember the answer:
        # clients that read structuredContent never see the content text
        # block, and only a declared key may be written back on a call.
        if data.options.enable_conversation_id:
            data.tool_output_instructions[tool.name] = (
                add_instructions_to_output_schema(tool)
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
        session_id = await prepare_request(
            data,
            mcp_session_id=mcp_session_id,
            client_name=client_name,
            client_version=client_version,
            protocol_version=protocol_version,
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
                protocol_version=protocol_version,
                extra=extra,
            )
            raise
        duration_ms = (time.monotonic() - start) * 1000
        tools = extract_tools(result)
        # Zero advertised tools is treated as an errored tools/list (parity with the
        # TS SDK), checked before we append our own get_more_tools virtual tool.
        empty = len(tools) == 0

        names = []
        for tool in tools:
            names.append(tool.name)
            if getattr(tool, "description", None):
                data.tool_descriptions[tool.name] = tool.description
            category = read_tool_category(tool)
            if category:
                data.tool_categories[tool.name] = category

        _inject_tool_schemas(server, data, tools)

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
            protocol_version=protocol_version,
            extra=extra,
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
