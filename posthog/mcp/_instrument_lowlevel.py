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

import functools
import inspect
import time
from functools import lru_cache
from typing import Any, Dict, List, Optional, Set, Tuple

import mcp.types as mcp_types

from ._context_parameters import is_context_enabled, schema_has_param
from ._conversation_id import build_prompt_back
from ._event_types import MCPAnalyticsEventType
from ._instrumentation import (
    _to_jsonable,
    advertised_tool_names,
    apply_virtual_tool_injection,
    collect_listed_tools,
    extract_tools,
    is_first_listing_page,
    mutate_tool_schema,
    prepare_request,
    raw_listing_owns_tool_name,
    record_resource_request,
    request_to_dict,
    resource_listing_response,
    resolve_session_and_client,
    resolve_virtual_tool_injection,
    start_tool_call_lifecycle,
    start_tools_list_lifecycle,
    warn_ownership_lookup_failed,
)
from ._internal import MCPAnalyticsData
from ._model_parameters import request_meta_from_context
from ._model_parameters import can_inject_model_parameter, is_capture_model_enabled
from ._output_instructions import mirror_instructions_into_structured_content
from .logger import log, warn
from .tools import get_more_tools_result_text

_WRAPPED_FLAG = "__posthog_mcp_wrapped__"


def instrument_low_level(server: Any, data: MCPAnalyticsData) -> None:
    """Instrument a raw ``mcp.server.Server``. ``context`` is injected as an
    optional schema property and NOT stripped — that schema is also the call's
    validation schema, and a typical ``(name, arguments)`` handler ignores extra keys."""
    data.server_name = getattr(server, "name", None)
    data.server_version = getattr(server, "version", None)
    _wrap_call_tool(server, data, strip_injected=False)
    _wrap_list_tools(server, data, context_required=False)
    _wrap_resource_requests(server, data)


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
    _wrap_list_tools(low_level, data, context_required=False, high_level=server)
    _wrap_resource_requests(low_level, data)


def _wrap_resource_requests(server: Any, data: MCPAnalyticsData) -> None:
    for request_type, event_type in (
        (mcp_types.ListResourcesRequest, MCPAnalyticsEventType.MCP_RESOURCES_LIST),
        # Templates are listings too: the captured request method separates
        # `resources/templates/list` from `resources/list` on the same event.
        (
            mcp_types.ListResourceTemplatesRequest,
            MCPAnalyticsEventType.MCP_RESOURCES_LIST,
        ),
        (mcp_types.ReadResourceRequest, MCPAnalyticsEventType.MCP_RESOURCES_READ),
    ):
        _wrap_resource_request(server, data, request_type, event_type)


def _wrap_resource_request(
    server: Any,
    data: MCPAnalyticsData,
    request_type: Any,
    event_type: str,
) -> None:
    handlers = server.request_handlers
    original = handlers.get(request_type)
    if original is None or getattr(original, _WRAPPED_FLAG, False):
        return

    async def handler(req: Any) -> Any:
        client_name, client_version = _client_info(server)
        protocol_version = _protocol_version(server)
        mcp_session_id = _mcp_session_id(server)
        token, client_name, client_version, protocol_version = (
            resolve_session_and_client(
                mcp_session_id, client_name, client_version, protocol_version
            )
        )
        request = request_to_dict(req)
        extra = {"session_id": mcp_session_id, "ctx": _request_context(server)}
        try:
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
        except Exception as error:  # noqa: BLE001 - analytics must not break resources
            log(f"Warning: could not prepare resource analytics: {error}")
            return await original(req)

        start = time.monotonic()
        try:
            result = await original(req)
        except Exception as error:
            await record_resource_request(
                data,
                session_id,
                event_type=event_type,
                request=request,
                error=error,
                duration_ms=(time.monotonic() - start) * 1000,
                client_name=client_name,
                client_version=client_version,
                protocol_version=protocol_version,
                extra=extra,
            )
            raise

        await record_resource_request(
            data,
            session_id,
            event_type=event_type,
            request=request,
            response=resource_listing_response(event_type, result),
            duration_ms=(time.monotonic() - start) * 1000,
            client_name=client_name,
            client_version=client_version,
            protocol_version=protocol_version,
            extra=extra,
        )
        return result

    setattr(handler, _WRAPPED_FLAG, True)
    handlers[request_type] = handler


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
        strip, model_ours = (
            await _standalone_ownership(data, high_level, name, req.params.meta)
            if strip_injected
            else (set(), data.tool_model_parameter_injected.get(name))
        )
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
            request_meta=request_meta_from_context(_request_context(server)),
            # Reads fail open on unknown ownership (posthog-js ADR-0011).
            allow_self_reported_model=model_ours is not False,
            mcp_session_id=mcp_session_id,
            token=token,
            client_name=client_name,
            client_version=client_version,
            protocol_version=protocol_version,
            extra={"session_id": mcp_session_id, "ctx": _request_context(server)},
        )

        if lifecycle.is_missing_capability and (
            await _name_owned_by_real_tool(high_level, data, name, server) is False
        ):
            virtual_content = [
                mcp_types.TextContent(type="text", text=text)
                for text in lifecycle.virtual_result_texts(get_more_tools_result_text())
            ]
            await lifecycle.record_missing_capability(conversation_id_delivered=True)
            return mcp_types.ServerResult(
                mcp_types.CallToolResult(
                    content=virtual_content,
                    isError=False,
                )
            )

        if lifecycle.is_feedback and (
            await _name_owned_by_real_tool(high_level, data, name, server) is False
        ):
            reply = await lifecycle.record_feedback(conversation_id_delivered=True)
            virtual_content = [
                mcp_types.TextContent(type="text", text=text)
                for text in lifecycle.virtual_result_texts(reply)
            ]
            return mcp_types.ServerResult(
                mcp_types.CallToolResult(
                    content=virtual_content,
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
        if strip and req.params.arguments:
            for key in strip:
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
    data: MCPAnalyticsData,
    tools: list,
    *,
    context_required: bool,
    high_level: Any = None,
) -> None:
    """Advertise the analytics parameters on a listing's tools, in place.

    Runs on both the client-facing listing and the SDK's internal cache-
    population pass, so the schema the SDK validates against always matches the
    one we advertised — see the note in ``handler``.
    """
    # Middleware can replace the registered tool on another replica. Without
    # proof of ownership there, advertising a field could break its validation.
    inject_model = high_level is None or not _dispatch_can_differ(high_level)
    verdicts: Dict[str, bool] = {}
    for tool in tools:
        schema = getattr(tool, "inputSchema", None)
        mutate_tool_schema(
            data,
            tool,
            schema_attribute="inputSchema",
            owns_context=schema_has_param(schema, "context"),
            context_required=context_required,
            is_sdk_virtual_tool=False,
            inject_model=inject_model,
        )
        verdict = data.tool_model_parameter_injected.get(tool.name)
        if verdict is None:
            continue
        if verdicts.setdefault(tool.name, verdict) != verdict:
            # Two advertised tools share this name and disagree (FastMCP 2.x
            # lists a middleware tool beside the registered one it shadows).
            # Which one dispatches is unknown, so the strip fails closed.
            data.tool_model_parameter_injected[tool.name] = False


def _wrap_list_tools(
    server: Any,
    data: MCPAnalyticsData,
    *,
    context_required: bool,
    high_level: Any = None,
) -> None:
    handlers = server.request_handlers
    original = handlers.get(mcp_types.ListToolsRequest)
    if original is None or getattr(original, _WRAPPED_FLAG, False):
        return

    async def probe_raw_tool_names(_ctx: Any = None) -> Optional[Set[str]]:
        """The names the host's own handler advertises on its first page. Calls
        ``original``, not the wrapper, so the probe never recurses into
        instrumentation or appends a virtual tool. Read by
        ``_name_owned_by_real_tool`` on raw low-level servers, which have no
        tool registry to ask instead."""
        # Late-bound on purpose: a host that replaces or removes tools/list
        # *after* instrument() leaves `original` holding a catalogue the client
        # never sees, and a confident answer from it would swallow a real tool.
        # Report the question as unanswerable instead, so the call is delegated.
        # `@posthog/mcp` re-captures the handler for the same reason.
        current = handlers.get(mcp_types.ListToolsRequest)
        if current is None or not getattr(current, _WRAPPED_FLAG, False):
            # The virtual tools stay advertised -- a handler chained in front
            # of ours still runs our injection -- but nothing is intercepted
            # behind them, and a silent stop is invisible in the captured data.
            if not data.warned_foreign_list_handler:
                data.warned_foreign_list_handler = True
                warn(
                    "Warning: your tools/list handler was replaced or removed "
                    "after instrument(), so PostHog can no longer tell whether a "
                    "tool "
                    "name is yours. Calls to PostHog's virtual tools are "
                    "delegated to your server, so no $mcp_missing_capability or "
                    "$mcp_feedback events are captured. Call instrument() after "
                    "registering your handlers."
                )
            return None
        result = await original(mcp_types.ListToolsRequest(method="tools/list"))
        tools = extract_tools(result)
        # `original` is usually the SDK's own list_tools decorator, which rebuilds
        # `Server._tool_cache` from these un-injected schemas every time it runs.
        # That cache is what the SDK validates real tool arguments against, so
        # without re-injecting here the next real call is rejected for sending the
        # `context` we advertised. Same reason the `req is None` branch below
        # injects.
        _inject_tool_schemas(
            data, tools, context_required=context_required, high_level=high_level
        )
        return advertised_tool_names(tools)

    data.raw_tool_names_probe = probe_raw_tool_names

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
            tools = extract_tools(result)
            _inject_tool_schemas(
                data, tools, context_required=context_required, high_level=high_level
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
        injection = resolve_virtual_tool_injection(
            data,
            tools,
            is_first_page=is_first_listing_page(getattr(req, "params", None)),
        )

        _inject_tool_schemas(
            data, tools, context_required=context_required, high_level=high_level
        )

        result = apply_virtual_tool_injection(
            result, injection, names, data, schema_field="inputSchema"
        )

        await lifecycle.record_result(
            names=names,
            response=_to_jsonable(result),
            duration_ms=duration_ms,
            is_empty=empty,
        )

        return result

    setattr(handler, _WRAPPED_FLAG, True)
    handlers[mcp_types.ListToolsRequest] = handler


async def _name_owned_by_real_tool(
    high_level: Any, data: MCPAnalyticsData, name: str, server: Any
) -> Optional[bool]:
    """Whether a real application tool owns ``name``, so a virtual tool never
    shadows it. Kind-agnostic: a lookup by name, shared by both virtual tools
    rather than twin helpers that can drift. The other two adapters keep the
    same tri-state contract against their own registries.

    On the standalone-fastmcp path the tool registry answers authoritatively. A
    raw low-level server has no registry, so it asks the host's own tools/list
    handler instead.

    Known limit, and *not* one a fallback can close: a ``FastMCP`` is an
    ``AggregateProvider``, which gathers its providers with
    ``return_exceptions=True`` and drops the failures, so an unreachable
    mounted or proxied sub-server reads back as a plain ``None`` --
    indistinguishable from "no such tool" -- and we treat the name as free.
    Consulting ``list_tools`` does not help: the same failure is dropped from
    the listing too (``_collect_list_results``), and fastmcp 3.x exposes no
    error strategy to opt out of. Narrower than it reads -- during the outage
    the host's tool is absent from ``tools/list`` as well, so only a provider
    that recovers between this check and dispatch loses a call that would have
    worked. The remedy stays the documented one: rename PostHog's tool."""
    if high_level is not None:
        try:
            return await high_level.get_tool(name) is not None
        except Exception as err:  # noqa: BLE001 - analytics must not break the call
            if isinstance(err, _tool_lookup_not_found_errors()):
                return False
            # The lookup failed rather than answered, so not "the name is free"
            # -- guessing that would swallow a real tool of theirs. What reaches
            # here is the visibility, transform and auth work layered on top of
            # the providers; a provider failure never does (see the docstring).
            warn_ownership_lookup_failed(name, err)
            return None
    # May be None: see `raw_listing_owns_tool_name`. Callers intercept only on a
    # definite False.
    return await raw_listing_owns_tool_name(data, name, server)


@lru_cache(maxsize=1)
def _tool_lookup_not_found_errors() -> Tuple[type, ...]:
    """The fastmcp exceptions that mean "no live tool by that name" -- an answer.
    Anything else out of ``get_tool`` is the lookup itself failing. Empty when
    fastmcp is absent or has moved them, which makes every failure delegate:
    the safe direction."""
    try:
        from fastmcp import exceptions
    except Exception:  # noqa: BLE001 - no fastmcp on this path
        return ()
    return tuple(
        err
        for err in (
            getattr(exceptions, "NotFoundError", None),
            getattr(exceptions, "DisabledError", None),
        )
        if isinstance(err, type) and issubclass(err, BaseException)
    )


_INJECTED_KEYS = ("context", "conversation_id", "llm_model")


async def _standalone_ownership(
    data: MCPAnalyticsData, high_level: Any, name: str, meta: Any
) -> Tuple[set, Optional[bool]]:
    """Ownership of the injected arguments on jlowin's standalone FastMCP: the
    keys to strip before it validates the call, and whether ``llm_model`` is
    ours (``None`` when nothing can say).

    Only keys injected under the current options are candidates. ``context``
    and ``conversation_id`` are stripped unless the registered schema (or,
    without one, the function signature) declares them. A registry failure
    protects all keys; a missing registry entry retains the middleware fallback
    for ``context`` and ``conversation_id``. Middleware that can replace a tool
    disables model injection and invalidates earlier model ownership. Otherwise
    the listing or registry decides; with neither witness the model argument
    stays and is still read (posthog-js ADR-0011).
    """
    try:
        declared, model_injectable = await _registry_view(high_level, name, meta)
        model_ours = data.tool_model_parameter_injected.get(name, model_injectable)
        if _dispatch_can_differ(high_level):
            model_ours = False
    except Exception:  # noqa: BLE001 - ownership inference must never prevent dispatch
        declared, model_ours = None, None
    candidates = _injected_keys(data)
    strip = {k for k in candidates - {"llm_model"} if k not in (declared or set())}
    if "llm_model" in candidates and model_ours:
        strip.add("llm_model")
    return strip, model_ours


def _injected_keys(data: MCPAnalyticsData) -> set:
    """The analytics arguments the SDK injects under the current options — the
    only ones it may strip. A disabled feature injects nothing, so its key is
    the application's even when the schema does not declare it."""
    keys = set()
    if is_context_enabled(data.options.context):
        keys.add("context")
    if data.options.enable_conversation_id:
        keys.add("conversation_id")
    if is_capture_model_enabled(data.options.capture_model):
        keys.add("llm_model")
    return keys


async def _registry_view(
    high_level: Any, name: str, meta: Any
) -> Tuple[Optional[set], Optional[bool]]:
    """What the registered tool says about the injected keys: which of
    ``_INJECTED_KEYS`` it declares itself, and whether a listing would have
    injected ``llm_model`` into its schema (the same test the listing applies,
    on the schema as the client would see it). Read from the schema (a ``Tool``
    subclass may have no function) else the signature. The registry is read
    directly, never through middleware, so a cold instance answers without a
    listing and rate limiters are not charged. ``(None, None)`` when the
    registry has no such tool or cannot be read."""
    try:
        tool = await _registered_tool(high_level, name, meta)
    except Exception as error:  # noqa: BLE001 - introspection is best-effort
        if not isinstance(error, _tool_lookup_not_found_errors()):
            warn_ownership_lookup_failed(name, error)
            return set(_INJECTED_KEYS), None
        return None, None
    if tool is None:
        return None, None
    schema = getattr(tool, "parameters", None)
    if isinstance(schema, dict):
        return _schema_view(schema, dereferenced=_server_dereferences(high_level))
    fn = getattr(tool, "fn", None)
    if fn is None:
        return set(), True
    try:
        declared = {k for k in _INJECTED_KEYS if k in inspect.signature(fn).parameters}
    except Exception:  # noqa: BLE001 - introspection is best-effort
        return set(), True
    return declared, "llm_model" not in declared


async def _registered_tool(high_level: Any, name: str, meta: Any) -> Any:
    """The tool version the request pinned in ``_meta.fastmcp.version``, else
    the highest one — the same choice FastMCP makes when dispatching."""
    version = _requested_tool_version(meta)
    if version is None:
        return await high_level.get_tool(name)
    from fastmcp.utilities.versions import VersionSpec

    return await high_level.get_tool(name, version=VersionSpec(eq=version))


def _requested_tool_version(meta: Any) -> Optional[str]:
    """Ownership must follow dispatch. Only a FastMCP that exposes the
    ``_meta`` version extractor its own dispatch uses honours a pinned
    version; every earlier release calls the highest version regardless."""
    try:
        from fastmcp.server.dependencies import extract_version_spec
    except ImportError:
        return None
    dump = getattr(meta, "model_dump", None)
    if callable(dump):
        meta = dump(by_alias=True)
    return extract_version_spec(meta) if isinstance(meta, dict) else None


def _schema_view(schema: Dict[str, Any], *, dereferenced: bool) -> Tuple[set, bool]:
    """The injected keys a schema declares as its own, and whether a listing
    would inject ``llm_model`` into it. With dereferencing on, every node along
    a root ``$ref`` chain counts, as the client sees the merged schema; with it
    off the client sees the reference itself, which the listing never injects
    into. Nothing is injected into a composed or unresolvable schema either, so
    all keys count as declared there and nothing is stripped."""
    nodes = _reference_chain(schema) if dereferenced else [schema]
    if nodes is None or any(
        node.get(key) for node in nodes for key in ("oneOf", "allOf", "anyOf")
    ):
        return set(_INJECTED_KEYS), False
    declared = {
        key
        for node in nodes
        if isinstance(node.get("properties"), dict)
        for key in node["properties"]
        if key in _INJECTED_KEYS
    }
    injectable = can_inject_model_parameter(nodes[-1]) and "llm_model" not in declared
    return declared, injectable


def _server_dereferences(server: Any) -> bool:
    """Whether this FastMCP dereferences schemas before advertising them (its
    built-in middleware, on by default from 3.x; absent on 2.x)."""
    return any(
        type(middleware).__module__.endswith(".dereference")
        for middleware in getattr(server, "middleware", ())
    )


_DISPATCH_HOOKS = ("on_message", "on_request", "on_list_tools", "on_call_tool")


def _dispatch_can_differ(server: Any) -> bool:
    """Whether application middleware can provide, shadow, or reroute a tool,
    making the registry an unreliable witness for what actually runs. Any
    override of a listing or dispatch hook can; FastMCP's own built-ins are
    excluded. Without a trustworthy registry a cold instance keeps ``llm_model``
    (strips fail closed), which is exactly what it did before this path."""
    try:
        from fastmcp.server.middleware import Middleware
    except ImportError:  # FastMCP before middleware existed: nothing can differ
        return False

    return any(
        type(middleware) not in _builtin_middleware_types()  # subclasses are the app's
        and any(
            getattr(type(middleware), hook) is not getattr(Middleware, hook)
            for hook in _DISPATCH_HOOKS
        )
        for middleware in getattr(server, "middleware", ())
    )


@functools.lru_cache(maxsize=1)
def _builtin_middleware_types() -> tuple:
    """The middleware a bare ``FastMCP()`` installs on its own, probed rather
    than named so a new built-in in a later release is still recognised."""
    from fastmcp import FastMCP

    return tuple(type(middleware) for middleware in FastMCP("posthog-probe").middleware)


def _reference_chain(schema: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """The schema and each local root ``$ref`` target in turn; ``None`` for a
    reference that is external, dangling, or cyclic."""
    nodes = [schema]
    while len(nodes) <= 8:
        ref = nodes[-1].get("$ref")
        if ref is None:
            return nodes
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return None
        target: Any = schema
        for part in ref[2:].split("/"):
            key = part.replace("~1", "/").replace("~0", "~")  # JSON Pointer escapes
            target = target.get(key) if isinstance(target, dict) else None
        if not isinstance(target, dict) or any(target is node for node in nodes):
            return None
        nodes.append(target)
    return None


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
