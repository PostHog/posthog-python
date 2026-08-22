# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""MCP Python SDK 2.x adapters (spec revision 2026-07-28).

Two entry points, mirroring the 1.x pair:

* :func:`instrument_mcpserver_v2` — the high-level ``mcp.server.mcpserver.MCPServer``
  (the renamed FastMCP). Tool calls are wrapped at ``ToolManager.call_tool``, the
  one seam every dispatch routes through (late-registered tools covered for
  free); tools/list at the underlying low-level registry.
* :func:`instrument_lowlevel_v2` — the low-level ``mcp.server.lowlevel.Server``.
  v2 replaced the public ``request_handlers`` dict (keyed by request class) with
  ``add_request_handler``/``get_request_handler`` keyed by method string, and
  handlers changed shape to ``(ctx, params)``; both existing registrations and
  later ``add_request_handler`` calls are wrapped (the posthog-js#4449 lesson:
  adapters that hand over a bare server register handlers *after* instrument()).

Era note: a single v2 server serves both protocol eras request by request — the
legacy 2025-11-25 handshake and the stateless 2026-07-28 envelope. Nothing here
branches on era: ``ctx.protocol_version`` is captured as-is, client identity
comes from ``ctx.session.client_params`` (synthesized from the per-request
envelope on the modern era), and on 2026-07-28 — which removed protocol
sessions — cross-pod correlation comes from ``enable_conversation_id``.

v2 models expose snake_case attributes (``is_error``, ``input_schema``,
``client_info``); the wire JSON keeps the camelCase aliases.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

import mcp.types as mcp_types

from ._context_parameters import schema_has_param
from ._conversation_id import build_prompt_back
from ._instrumentation import (
    _to_jsonable,
    collect_listed_tools,
    mutate_tool_schema,
    params_to_request_dict,
    resolve_session_and_client,
    start_tool_call_lifecycle,
    start_tools_list_lifecycle,
)
from ._internal import MCPAnalyticsData
from ._output_instructions import mirror_instructions_into_structured_content
from .logger import log
from .request_headers import get_request_headers
from .session_token import read_mcp_session_header
from .tools import (
    build_report_missing_descriptor,
    get_more_tools_result_text,
    resolve_missing_capability_tool_name,
)

_WRAPPED_FLAG = "__posthog_mcp_wrapped__"

# The two methods we instrument, and how the tools/list wrapper treats the
# injected `context` parameter per entry point (see _wrap_v2_list_tools).
_CALL_METHOD = "tools/call"
_LIST_METHOD = "tools/list"


def instrument_mcpserver_v2(server: Any, data: MCPAnalyticsData) -> None:
    """Instrument a v2 ``MCPServer``. Injected ``context``/``conversation_id``
    are STRIPPED before dispatch (v2 validates tool arguments against the
    function signature and rejects unexpected keys), unless the tool's own
    schema declares the parameter — then it's a real argument the agent's value
    belongs to."""
    low_level = getattr(server, "_lowlevel_server", None)
    if low_level is None:
        log("Warning: MCPServer has no _lowlevel_server; cannot instrument.")
        return
    data.server_name = getattr(server, "name", None) or getattr(low_level, "name", None)
    data.server_version = getattr(server, "version", None) or getattr(
        low_level, "version", None
    )
    _wrap_tool_manager_call_v2(server, data)
    _wrap_v2_list_tools(low_level, data, context_required=True, high_level=server)
    _patch_add_request_handler(low_level, data, wrap_call=False, high_level=server)


def instrument_lowlevel_v2(server: Any, data: MCPAnalyticsData) -> None:
    """Instrument a raw v2 low-level ``Server``. ``context`` is injected as an
    *optional* schema property and NOT stripped — the schema doubles as the
    call's validation surface, and a typical ``(ctx, params)`` handler ignores
    extra argument keys."""
    data.server_name = getattr(server, "name", None)
    data.server_version = getattr(server, "version", None)
    _wrap_v2_call_tool(server, data)
    _wrap_v2_list_tools(server, data, context_required=False)
    _patch_add_request_handler(server, data, wrap_call=True)


# --- registry plumbing ---------------------------------------------------------


def _replace_handler(server: Any, method: str, wrapped: Any, params_type: Any) -> None:
    """Re-register through the public API so the SDK keeps owning validation."""
    server.add_request_handler(method, params_type, wrapped)


def _patch_add_request_handler(
    server: Any, data: MCPAnalyticsData, *, wrap_call: bool, high_level: Any = None
) -> None:
    """Wrap ``add_request_handler`` so handlers registered *after* instrument()
    for the instrumented methods get wrapped too. Registrations for other
    methods pass through untouched."""
    original_add = server.add_request_handler
    if getattr(original_add, _WRAPPED_FLAG, False):
        return

    def add_request_handler(method: str, params_type: Any, handler: Any) -> None:
        original_add(method, params_type, handler)
        if getattr(handler, _WRAPPED_FLAG, False):
            return
        if method == _CALL_METHOD and wrap_call:
            _wrap_v2_call_tool(server, data)
        elif method == _LIST_METHOD:
            _wrap_v2_list_tools(
                server,
                data,
                context_required=high_level is not None,
                high_level=high_level,
            )

    setattr(add_request_handler, _WRAPPED_FLAG, True)
    server.add_request_handler = add_request_handler


# --- ctx readers -----------------------------------------------------------------


def _request_context_of(context: Any) -> Any:
    """The request context behind a v2 ``Context``, or ``None``.

    Reads the public property rather than the private ``_request_context`` it
    wraps, guarded because it *raises* outside a request (the same trap the
    FastMCP adapter hit). Falls back to the private attribute so a stand-in
    object that only carries that still works.
    """
    try:
        return context.request_context
    except (LookupError, ValueError, AttributeError):
        return getattr(context, "_request_context", None)


def _ctx_client_info(ctx: Any) -> Tuple[Optional[str], Optional[str]]:
    try:
        client_params = ctx.session.client_params
        info = getattr(client_params, "client_info", None)
        if info is not None:
            return getattr(info, "name", None), getattr(info, "version", None)
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _ctx_protocol_version(ctx: Any) -> Optional[str]:
    version = getattr(ctx, "protocol_version", None)
    if isinstance(version, str) and version:
        return version
    try:
        return ctx.session.client_params.protocol_version
    except Exception:  # noqa: BLE001
        return None


def _ctx_mcp_session_id(ctx: Any) -> Optional[str]:
    """Best-effort transport session id (the ``Mcp-Session-Id`` header on the
    legacy era — 2026-07-28 removed it). ``ctx.request`` carries the transport's
    HTTP request when there is one; ``None`` on stdio.

    Reuses the same header-bag normalisation and case-insensitive lookup the
    public ``get_request_headers``/``read_mcp_session_header`` helpers already
    provide, instead of a second hand-rolled ``ctx.request.headers`` read."""
    return read_mcp_session_header(get_request_headers(ctx))


def _resolve_ctx(
    ctx: Any,
) -> Tuple[Optional[Any], Optional[str], Optional[str], Optional[str], Optional[str]]:
    """(token, client_name, client_version, protocol_version, mcp_session_id)
    for a request, with token-carried identity backfilled for stateless pods."""
    client_name, client_version = _ctx_client_info(ctx)
    protocol_version = _ctx_protocol_version(ctx)
    mcp_session_id = _ctx_mcp_session_id(ctx)
    token, client_name, client_version, protocol_version = resolve_session_and_client(
        mcp_session_id, client_name, client_version, protocol_version
    )
    return token, client_name, client_version, protocol_version, mcp_session_id


# --- tool ownership --------------------------------------------------------------


def _tool_own_properties_v2(high_level: Any, name: str) -> Dict[str, Any]:
    """The tool's own declared JSON Schema ``properties``, read once per call
    site so checking ownership of both ``context`` and ``conversation_id``
    doesn't look the tool up from the manager twice."""
    try:
        tool = high_level._tool_manager.get_tool(name)
        properties = (getattr(tool, "parameters", None) or {}).get("properties")
    except Exception:  # noqa: BLE001
        return {}
    # Fail closed on a malformed schema: the caller does `param in <this>` in the
    # tool-call hot path, where a None or a string would raise or answer by
    # substring.
    return properties if isinstance(properties, dict) else {}


def _tool_owns_param_v2(high_level: Any, name: str, param: str) -> bool:
    """Whether the tool's own JSON schema declares ``param`` — then it's a real
    tool argument we must neither inject over nor strip. Read from the tool's
    declared parameters rather than the function signature so a tool taking the
    SDK's ``Context`` object under a ``context`` name isn't mistaken for owning
    our string parameter."""
    return param in _tool_own_properties_v2(high_level, name)


# --- high-level: ToolManager.call_tool seam --------------------------------------


def _wrap_tool_manager_call_v2(server: Any, data: MCPAnalyticsData) -> None:
    tool_manager = getattr(server, "_tool_manager", None)
    if tool_manager is None:
        log("Warning: MCPServer has no _tool_manager; tool calls will not be captured.")
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
        ctx = _request_context_of(context)
        token, client_name, client_version, protocol_version, mcp_session_id = (
            _resolve_ctx(ctx)
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
            extra={"session_id": mcp_session_id, "ctx": ctx},
        )

        if lifecycle.is_missing_capability:
            await lifecycle.record_missing_capability()
            return mcp_types.CallToolResult(
                content=[
                    mcp_types.TextContent(
                        type="text", text=get_more_tools_result_text()
                    )
                ]
            )

        # v2 validates against the function signature and rejects unexpected
        # keys, so injected parameters are stripped before dispatch — but never
        # one the tool's own schema declares (that's a real argument).
        call_arguments = arguments
        if isinstance(arguments, dict):
            own_properties = _tool_own_properties_v2(server, name)
            strip_keys = set()
            if "context" not in own_properties:
                strip_keys.add("context")
            if (
                data.options.enable_conversation_id
                and "conversation_id" not in own_properties
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
            # The outer MCPServer layer converts the raise after this seam; no
            # freshly minted handle could have been delivered yet.
            await lifecycle.record_error(error, (time.monotonic() - start) * 1000)
            raise
        duration_ms = (time.monotonic() - start) * 1000

        delivered = False
        if lifecycle.conversation_id:
            result, delivered = _deliver_conversation_id(
                data,
                result,
                name,
                lifecycle.conversation_id,
                lifecycle.minted_conversation_id,
            )

        await lifecycle.record_result(
            result, duration_ms, conversation_id_delivered=delivered
        )
        return result

    setattr(wrapped, _WRAPPED_FLAG, True)
    tool_manager.call_tool = wrapped


def _append_prompt_back(result: Any, conversation_id: str) -> Tuple[Any, bool]:
    """Append the conversation prompt-back to a result's ``content`` list (model
    or dict shape). Errored results included on purpose — a first-call failure
    is exactly when the agent needs the handle. Returns False for shapes with no
    content list to ride (e.g. MRTR ``input_required`` results). Returns
    ``(result, delivered)`` — the result may be a copy, never a mutation."""
    block = mcp_types.TextContent(
        type="text", text=build_prompt_back(conversation_id)["text"]
    )
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            return {**result, "content": [*content, block]}, True
        return result, False
    content = getattr(result, "content", None)
    if not isinstance(content, list):
        return result, False
    # Copy rather than append in place: a shared or cached result object would
    # otherwise accumulate a block per conversation and hand each caller the
    # previous callers' handles.
    copy_model = getattr(result, "model_copy", None)
    if callable(copy_model):
        try:
            return copy_model(update={"content": [*content, block]}), True
        except Exception:  # noqa: BLE001 - never let delivery break the tool path
            return result, False
    content.append(block)
    return result, True


def _deliver_conversation_id(
    data: MCPAnalyticsData, result: Any, name: str, conversation_id: str, minted: bool
) -> Tuple[Any, bool]:
    """Hand the conversation handle back over both channels a result has:
    mirrored into ``structuredContent`` on every response (for tools whose
    output schema we declared the key on), and as a ``content`` text block on
    the minting response only. Returns ``(result, delivered)`` — a minted handle
    the agent never received must not be stamped on the event."""
    delivered = False
    if data.tool_output_instructions.get(name):
        result, delivered = mirror_instructions_into_structured_content(
            result, conversation_id
        )
    if minted:
        result, appended = _append_prompt_back(result, conversation_id)
        delivered = delivered or appended
    return result, delivered


# --- low-level: tools/call ------------------------------------------------------


def _wrap_v2_call_tool(server: Any, data: MCPAnalyticsData) -> None:
    entry = server.get_request_handler(_CALL_METHOD)
    if entry is None or getattr(entry.handler, _WRAPPED_FLAG, False):
        return
    original = entry.handler

    async def handler(ctx: Any, params: Any) -> Any:
        name = params.name
        arguments = dict(params.arguments or {})
        token, client_name, client_version, protocol_version, mcp_session_id = (
            _resolve_ctx(ctx)
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
            extra={"session_id": mcp_session_id, "ctx": ctx},
        )

        if lifecycle.is_missing_capability:
            await lifecycle.record_missing_capability()
            return mcp_types.CallToolResult(
                content=[
                    mcp_types.TextContent(
                        type="text", text=get_more_tools_result_text()
                    )
                ]
            )

        # Settle the shared session before the tool body runs, so an in-tool
        # `analytics.capture()` is attributed to this caller and not the last one.
        await lifecycle.prime_session()

        start = time.monotonic()
        try:
            result = await original(ctx, params)
        except Exception as error:
            # Raw v2 handlers raise through to JSON-RPC errors; no freshly minted
            # handle could have been delivered yet.
            await lifecycle.record_error(error, (time.monotonic() - start) * 1000)
            raise
        duration_ms = (time.monotonic() - start) * 1000

        delivered = False
        if lifecycle.conversation_id:
            result, delivered = _deliver_conversation_id(
                data,
                result,
                name,
                lifecycle.conversation_id,
                lifecycle.minted_conversation_id,
            )

        await lifecycle.record_result(
            result, duration_ms, conversation_id_delivered=delivered
        )
        return result

    setattr(handler, _WRAPPED_FLAG, True)
    _replace_handler(server, _CALL_METHOD, handler, entry.params_type)


# --- tools/list -------------------------------------------------------------------


def _wrap_v2_list_tools(
    server: Any,
    data: MCPAnalyticsData,
    *,
    context_required: bool,
    high_level: Any = None,
) -> None:
    entry = server.get_request_handler(_LIST_METHOD)
    if entry is None or getattr(entry.handler, _WRAPPED_FLAG, False):
        return
    original = entry.handler

    async def handler(ctx: Any, params: Any) -> Any:
        token, client_name, client_version, protocol_version, mcp_session_id = (
            _resolve_ctx(ctx)
        )
        request = params_to_request_dict(_LIST_METHOD, params, by_alias=True)
        extra: Dict[str, Any] = {"session_id": mcp_session_id, "ctx": ctx}
        # Resolve session, emit $mcp_initialize (once per session) and identify
        # here too — a client may list tools without ever calling one.
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
            result = await original(ctx, params)
        except Exception as error:
            await lifecycle.record_error(error, (time.monotonic() - start) * 1000)
            raise
        duration_ms = (time.monotonic() - start) * 1000

        tools = list(getattr(result, "tools", []) or [])
        # Empty is computed before adding the virtual missing-capability tool.
        names, empty = collect_listed_tools(data, tools)

        for tool in tools:
            schema = getattr(tool, "input_schema", None)
            owns_context = (
                _tool_owns_param_v2(high_level, tool.name, "context")
                if high_level is not None
                else schema_has_param(schema, "context")
            )
            mutate_tool_schema(
                data,
                tool,
                schema_attribute="input_schema",
                owns_context=owns_context,
                context_required=context_required,
            )

        if data.options.report_missing:
            missing_name = resolve_missing_capability_tool_name(data.options)
            if not any(t.name == missing_name for t in tools):
                _append_get_more_tools_v2(result, missing_name)
                names.append(missing_name)

        await lifecycle.record_result(
            names=names,
            response=_to_jsonable(result),
            duration_ms=duration_ms,
            is_empty=empty,
        )

        return result

    setattr(handler, _WRAPPED_FLAG, True)
    _replace_handler(server, _LIST_METHOD, handler, entry.params_type)


def _append_get_more_tools_v2(result: Any, name: str) -> None:
    descriptor = build_report_missing_descriptor(name)
    tool = mcp_types.Tool(
        name=descriptor["name"],
        description=descriptor["description"],
        input_schema=descriptor["inputSchema"],
        annotations=descriptor["annotations"],
    )
    tools_list = getattr(result, "tools", None)
    if isinstance(tools_list, list):
        tools_list.append(tool)
