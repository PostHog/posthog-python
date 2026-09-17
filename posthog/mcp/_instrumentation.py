# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Shared MCP request lifecycles used by both the FastMCP and low-level server
adapters. The adapters resolve transport-specific details (client info, session
id, raw result shape) and delegate analytics policy here so both stay in sync."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Set

from ._capture import capture_event
from ._context_parameters import (
    add_context_parameter_to_schema,
    get_context_description,
    is_context_enabled,
    schema_has_param,
)
from ._conversation_id import (
    add_conversation_id_to_schema,
    build_prompt_back,
    resolve_conversation_id,
)
from ._event_types import MCPAnalyticsEventType
from ._exceptions import capture_exception
from .feedback import (
    build_feedback_event_properties,
    build_feedback_intent,
    get_feedback_tool_descriptor,
    handle_feedback,
    parse_feedback_report,
    resolve_collect_feedback_options,
    resolve_send_feedback_tool_name,
)
from ._intent import resolve_tool_call_intent, set_event_intent
from ._internal import MCPAnalyticsData, handle_identify, resolve_event_properties
from ._model_parameters import (
    add_model_parameter_to_schema,
    get_model_description,
    is_capture_model_enabled,
    resolve_model,
)
from ._output_instructions import add_instructions_to_output_schema
from .logger import log, warn
from .request_headers import get_request
from ._sanitization import build_captured_mcp_parameters
from ._transport_identity import stamp_transport_identity
from .session import resolve_session_id, resolve_session_id_with_source
from .session_token import SessionTokenPayload, decode_session_id
from .tools import resolve_missing_capability_tool_name
from .types import CollectFeedbackOptions, FeedbackReport

# The virtual tools this SDK advertises into tools/list. Every piece of per-tool
# policy -- enable switch, configured name, warning text, warn-once
# bookkeeping, warning text -- is keyed by kind so the two can't drift apart.
VIRTUAL_TOOL_MISSING_CAPABILITY = "missing_capability"
VIRTUAL_TOOL_FEEDBACK = "feedback"

# Why a collision happened, which picks the warning's wording. Named so a typo
# is a type error instead of silently getting the "blocked" text.
VirtualToolCollisionVariant = Literal["blocked", "duplicate", "shadowed"]

# The option that renames each virtual tool, quoted verbatim in the collision
# warnings.
_VIRTUAL_TOOL_RENAME_OPTION = {
    VIRTUAL_TOOL_MISSING_CAPABILITY: 'MCPAnalyticsOptions(missing_capability_tool_name="...")',
    VIRTUAL_TOOL_FEEDBACK: 'MCPAnalyticsOptions(collect_feedback=CollectFeedbackOptions(tool_name="..."))',
}
_VIRTUAL_TOOL_EVENT = {
    VIRTUAL_TOOL_MISSING_CAPABILITY: "$mcp_missing_capability",
    VIRTUAL_TOOL_FEEDBACK: "$mcp_feedback",
}

# Keep strong refs to in-flight capture tasks/futures and their lifecycle owners so
# they aren't GC'd mid-flight and lifecycle drains can select only their own work.
_BACKGROUND_TASKS: Dict[Any, Any] = {}
_tasks_lock = threading.Lock()

# A single daemon event loop for hosts with no running loop (sync dispatchers
# like PostHogMCP). Created lazily and reused, so we never leak a loop per call.
_bg_loop: Optional[asyncio.AbstractEventLoop] = None
_bg_loop_lock = threading.Lock()


def _reinit_background_loop_after_fork() -> None:
    """Drop background-loop state inherited by a forked child.

    The loop's daemon thread does not survive ``fork()``, and its lock may have
    been held by a vanished thread. Replace the state without acquiring the old
    lock or trying to close the inherited loop, which can no longer be driven.
    """
    global _BACKGROUND_TASKS, _tasks_lock, _bg_loop, _bg_loop_lock
    _BACKGROUND_TASKS = {}
    _tasks_lock = threading.Lock()
    _bg_loop = None
    _bg_loop_lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reinit_background_loop_after_fork)


def _get_background_loop() -> asyncio.AbstractEventLoop:
    global _bg_loop
    if _bg_loop is None:
        with _bg_loop_lock:
            if _bg_loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(
                    target=loop.run_forever, name="posthog-mcp-capture", daemon=True
                ).start()
                _bg_loop = loop
    return _bg_loop


def _track_task(task: Any, owner: Any) -> None:
    with _tasks_lock:
        _BACKGROUND_TASKS[task] = owner
    task.add_done_callback(_on_task_done)


def _on_task_done(task: Any) -> None:
    with _tasks_lock:
        _BACKGROUND_TASKS.pop(task, None)
    try:
        if not task.cancelled() and task.exception() is not None:
            log(f"background capture task failed: {task.exception()}")
    except Exception:  # noqa: BLE001 - never let bookkeeping raise
        pass


def fire_and_forget(
    coro: Optional[Any], owner: Any, *, background: bool = False
) -> None:
    """Schedule capture work and associate it with its lifecycle owner.

    Async instrumentation uses its current loop. Sync-only owners can request the
    shared background loop so their synchronous lifecycle methods can safely drain
    captures even when invoked by a host that also has a running event loop.
    """
    if coro is None:
        return
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None

    if background or running_loop is None:
        loop = _get_background_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        _track_task(future, owner)
        return

    task = running_loop.create_task(coro)
    _track_task(task, owner)


async def drain_pending(owner: Any) -> None:
    """Await this owner's in-flight captures bound to the current event loop."""
    loop = asyncio.get_running_loop()
    with _tasks_lock:
        tasks = [
            task
            for task, task_owner in _BACKGROUND_TASKS.items()
            if task_owner is owner
            and isinstance(task, asyncio.Task)
            and task.get_loop() is loop
            and not task.done()
        ]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def drain_pending_sync(owner: Any, timeout: Optional[float] = None) -> None:
    """Block until this owner's shared-background-loop captures finish."""
    with _tasks_lock:
        futures = [
            task
            for task, task_owner in _BACKGROUND_TASKS.items()
            if task_owner is owner
            and isinstance(task, concurrent.futures.Future)
            and not task.done()
        ]
    if futures:
        concurrent.futures.wait(futures, timeout=timeout)


def is_tool_result_error(result: Any) -> bool:
    """MCP tool results signal errors via ``isError: true`` rather than raising.
    The attribute is ``isError`` on MCP SDK 1.x models and ``is_error`` on 2.x
    (wire JSON unchanged); check both shapes."""
    if isinstance(result, dict):
        return result.get("isError") is True or result.get("is_error") is True
    is_error = getattr(result, "is_error", None)
    if is_error is not None:
        return is_error is True
    return getattr(result, "isError", None) is True


def build_tool_call_request(
    name: str, arguments: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    return {
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }


def _to_jsonable(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        # by_alias so captured payloads keep the camelCase wire shape on both MCP
        # SDK majors (2.x renamed model attributes to snake_case but kept the
        # aliases); 1.x field names are already the wire names, so this is a no-op.
        try:
            return obj.model_dump(mode="json", by_alias=True)
        except Exception:  # noqa: BLE001
            return str(obj)
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(item) for item in obj]
    if isinstance(obj, dict):
        return {key: _to_jsonable(value) for key, value in obj.items()}
    return obj


def _wrap_response(result: Any) -> Any:
    """Shape a tool result into the ``{content: [...]}`` form the sanitizer
    understands (so image/audio/blob blocks get redacted)."""
    serialized = _to_jsonable(result)
    if isinstance(serialized, list):
        return {"content": serialized}
    return serialized


async def _maybe_emit_initialize(
    data: MCPAnalyticsData,
    session_id: str,
    client_name: Optional[str],
    client_version: Optional[str],
    extra: Optional[Dict[str, Any]],
    protocol_version: Optional[str] = None,
) -> None:
    """Lazily emit ``$mcp_initialize`` once per session. The Python MCP SDK handles
    ``InitializeRequest`` inside the session layer (not ``request_handlers``), so we
    synthesize the event from the first instrumented request that carries client info."""
    if session_id in data.initialized_sessions:
        return
    data.mark_session_initialized(session_id)
    event: Dict[str, Any] = {
        "event_type": MCPAnalyticsEventType.MCP_INITIALIZE,
        "session_id": session_id,
        "client_name": client_name,
        "client_version": client_version,
        "protocol_version": protocol_version,
        "timestamp": datetime.now(timezone.utc),
    }
    await _apply_event_properties(
        data, event, {"method": "initialize", "params": {}}, extra
    )
    stamp_transport_identity(event, extra)
    fire_and_forget(capture_event(data, event), data)


async def _apply_event_properties(
    data: MCPAnalyticsData,
    event: Dict[str, Any],
    request: Dict[str, Any],
    extra: Optional[Dict[str, Any]],
) -> None:
    """Resolve the customer's ``event_properties`` callback and stamp it onto the
    event — applied to every auto-captured event type, matching the TS SDK."""
    props = await resolve_event_properties(data, request, extra)
    if props is not None:
        event["properties"] = props


def resolve_session_and_client(
    raw_session_id: Optional[str],
    client_name: Optional[str],
    client_version: Optional[str],
    protocol_version: Optional[str] = None,
) -> tuple[Optional[SessionTokenPayload], Optional[str], Optional[str], Optional[str]]:
    """Decode a replayed ``Mcp-Session-Id`` value as a self-encoded session token,
    and backfill the client name/version/protocol version from it when the live
    transport supplied none (the stateless-pod case, where ``initialize`` was never
    seen here).

    Returns ``(token, client_name, client_version, protocol_version)``; ``token`` is
    ``None`` when the header isn't one of our tokens (a plain UUID, JWT, or nothing)."""
    token = decode_session_id(raw_session_id)
    if token is not None:
        client_name = client_name or token.client_name
        client_version = client_version or token.client_version
        protocol_version = protocol_version or token.protocol_version
    return token, client_name, client_version, protocol_version


async def prime_session(
    data: MCPAnalyticsData,
    *,
    mcp_session_id: Optional[str],
    token: Optional[SessionTokenPayload] = None,
) -> None:
    """Point the shared per-server session at *this* request before the tool body runs.

    ``McpAnalytics.capture()`` reads ``data.session_id`` for custom in-tool
    events. The conversation anchor can only be resolved after the call (we
    don't know until then whether the agent received the handle), so without
    this the tool body would read whatever the *previous* request left behind
    and attribute a custom event to the wrong caller. Emits nothing — it only
    settles the transport/memory session an in-tool event should belong to.
    """
    await resolve_session_id(data, mcp_session_id, token=token)


def _is_sse_request(extra: Optional[Dict[str, Any]]) -> bool:
    """True for the deprecated SSE transport, which carries its session as a
    ``session_id`` query parameter rather than a header.

    Such a request resolves to a ``generated`` session for a reason the stateless
    mint cannot fix -- the mint sets a response header an SSE client never replays --
    so :func:`_warn_stateless_session_not_wired` would be recommending a remedy that
    does not apply."""
    try:
        params = getattr(get_request(extra), "query_params", None)
        return bool(params is not None and params.get("session_id"))
    except Exception:  # noqa: BLE001 - a transport probe must never break a tool call
        return False


def _warn_stateless_session_not_wired(data: MCPAnalyticsData) -> None:
    """Warn once per server when a tool call/listing arrives over HTTP but the
    session still had to come from this process's memory.

    That is the fingerprint of a stateless/multi-pod server whose mint middleware
    never attached — most often because the ASGI app was built (or mounted from
    another module) *before* ``instrument()`` ran, so wrapping the app factories
    couldn't retrofit the already-built app. The result is a silently fragmented
    ``$session_id``; this makes that failure loud instead of dark-in-prod."""
    if data.warned_no_stateless_session:
        return
    data.warned_no_stateless_session = True
    warn(
        "Warning: an MCP tool request arrived over streamable HTTP with no session id, so "
        "PostHog generated a per-process $session_id that will fragment across requests "
        "and pods. This usually means PostHogMcpStatelessSessionMiddleware never attached "
        "— e.g. the ASGI app was built or mounted before instrument() ran. If you build "
        "the app yourself, add the middleware explicitly: "
        "app.add_middleware(PostHogMcpStatelessSessionMiddleware). "
        "Enabling conversation ids (MCPAnalyticsOptions(enable_conversation_id=True)) also "
        "anchors the session without any middleware. "
        "See posthog/mcp/README.md (stateless / multi-pod servers)."
    )


async def prepare_request(
    data: MCPAnalyticsData,
    *,
    mcp_session_id: Optional[str],
    client_name: Optional[str],
    client_version: Optional[str],
    request: Dict[str, Any],
    extra: Optional[Dict[str, Any]],
    token: Optional[SessionTokenPayload] = None,
    protocol_version: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> str:
    """Resolve the session id, run identify, then lazily emit initialize. Returns
    the session id to stamp on the event for this request.

    ``conversation_id`` is the agent's handle for this request, and when present
    it anchors the session (ADR-0004) so every event of the request — identify,
    initialize, and the call itself — lands in the conversation's session rather
    than this instance's.

    Callers pass it only for a handle the agent **echoed**. A freshly minted one
    is unproven: this runs before the call, so delivery cannot be known yet, and
    if the prompt-back turns out to be undeliverable (an exception converted
    outside our seam, a result with nothing to carry it) the events would strand
    in a session nobody holds while the next call mints another — one orphan
    session per call, worse than not anchoring at all. An echo is the only proof
    of delivery, so the minting call stays in the transport/memory session and
    everything after it anchors.

    ``token`` is the decoded self-encoded session token (see ``session_token.py``);
    when present it takes precedence over ``mcp_session_id`` and carries the client
    identity across stateless pods.

    Identify runs *before* initialize so the resolved identity is already in the cache
    when ``capture_event`` builds the initialize event — otherwise the first
    ``$mcp_initialize`` is anonymous even when identify resolves on the same request.
    (Still not byte-parity with the TS SDK, which wraps the real initialize handler;
    the Python SDK handles initialize in the session layer, not ``request_handlers``.)

    A request that reached us over HTTP yet still resolved to this process's memory
    has nothing correlating it across pods, which on a stateless server means the
    mint middleware never attached — warn once rather than fragment silently."""
    session_id, session_source = await resolve_session_id_with_source(
        data, mcp_session_id, token=token, conversation_id=conversation_id
    )
    if (
        session_source == "generated"
        and get_request(extra) is not None
        and not _is_sse_request(extra)
    ):
        _warn_stateless_session_not_wired(data)
    identify_event = await handle_identify(data, session_id, request, extra)
    if identify_event:
        fire_and_forget(capture_event(data, identify_event), data)
    await _maybe_emit_initialize(
        data, session_id, client_name, client_version, extra, protocol_version
    )
    return session_id


@dataclass(frozen=True)
class ToolCallLifecycle:
    """Common analytics policy for one tool call.

    Adapters still own request-context lookup, argument stripping, dispatch, result
    shape, and conversation-id delivery. This object only keeps the shared session,
    missing-capability, and capture ordering in one place.
    """

    data: MCPAnalyticsData
    name: str
    arguments: Optional[Dict[str, Any]]
    request_meta: Optional[Dict[str, Any]]
    allow_self_reported_model: bool
    request: Dict[str, Any]
    extra: Dict[str, Any]
    mcp_session_id: Optional[str]
    token: Optional[SessionTokenPayload]
    client_name: Optional[str]
    client_version: Optional[str]
    protocol_version: Optional[str]
    # ``None`` when the virtual tool is disabled, so every downstream decision
    # treats a real tool by that name like any other tool's.
    missing_name: Optional[str]
    feedback_options: Optional[CollectFeedbackOptions]
    feedback_name: Optional[str]
    conversation_id: Optional[str]
    minted_conversation_id: bool

    @property
    def is_missing_capability(self) -> bool:
        return self.missing_name is not None and self.name == self.missing_name

    @property
    def is_feedback(self) -> bool:
        return self.feedback_name is not None and self.name == self.feedback_name

    async def prepare_session(self, conversation_id: Optional[str]) -> str:
        return await prepare_request(
            self.data,
            mcp_session_id=self.mcp_session_id,
            client_name=self.client_name,
            client_version=self.client_version,
            protocol_version=self.protocol_version,
            request=self.request,
            extra=self.extra,
            token=self.token,
            conversation_id=conversation_id,
        )

    async def prime_session(self) -> None:
        await prime_session(
            self.data, mcp_session_id=self.mcp_session_id, token=self.token
        )

    def virtual_result_texts(self, primary_text: str) -> List[str]:
        """Build the text payload for an SDK virtual-tool result."""
        if not self.conversation_id or not self.minted_conversation_id:
            return [primary_text]
        return [primary_text, build_prompt_back(self.conversation_id)["text"]]

    def _anchored_conversation_id(self, delivered: bool) -> Optional[str]:
        if self.minted_conversation_id and not delivered:
            return None
        return self.conversation_id

    async def record_missing_capability(
        self, *, conversation_id_delivered: bool = False
    ) -> None:
        conversation_id = self._anchored_conversation_id(conversation_id_delivered)
        session_id = await self.prepare_session(conversation_id)
        await record_missing_capability(
            self.data,
            session_id,
            conversation_id=conversation_id,
            tool_name=self.missing_name or self.name,
            context=(self.arguments or {}).get("context"),
            arguments=self.arguments,
            request_meta=self.request_meta,
            allow_self_reported_model=True,
            client_name=self.client_name,
            client_version=self.client_version,
            protocol_version=self.protocol_version,
            extra=self.extra,
        )

    async def record_feedback(self, *, conversation_id_delivered: bool = False) -> str:
        """Capture the ``$mcp_feedback`` event, then run the host's ``on_feedback``
        handler and return the reply text for the agent. The event is captured
        whether or not the handler raises."""
        report = parse_feedback_report(self.arguments, self.feedback_options)
        conversation_id = self._anchored_conversation_id(conversation_id_delivered)
        session_id = await self.prepare_session(conversation_id)
        await record_feedback(
            self.data,
            session_id,
            conversation_id=conversation_id,
            report=report,
            tool_name=self.feedback_name or self.name,
            arguments=self.arguments,
            request_meta=self.request_meta,
            client_name=self.client_name,
            client_version=self.client_version,
            protocol_version=self.protocol_version,
            extra=self.extra,
        )
        return await handle_feedback(report, self.feedback_options)

    async def record_error(self, error: Any, duration_ms: float) -> None:
        # A freshly minted handle cannot anchor or be captured when dispatch
        # raised: no adapter had an opportunity to deliver it to the agent.
        conversation_id = self._anchored_conversation_id(False)
        session_id = await self.prepare_session(conversation_id)
        await record_tool_call(
            self.data,
            session_id,
            name=self.name,
            arguments=self.arguments,
            request_meta=self.request_meta,
            allow_self_reported_model=self.allow_self_reported_model,
            error=error,
            duration_ms=duration_ms,
            client_name=self.client_name,
            client_version=self.client_version,
            protocol_version=self.protocol_version,
            conversation_id=conversation_id,
            extra=self.extra,
        )

    async def record_result(
        self, result: Any, duration_ms: float, *, conversation_id_delivered: bool
    ) -> None:
        conversation_id = self._anchored_conversation_id(conversation_id_delivered)
        session_id = await self.prepare_session(conversation_id)
        await record_tool_call(
            self.data,
            session_id,
            name=self.name,
            arguments=self.arguments,
            request_meta=self.request_meta,
            allow_self_reported_model=self.allow_self_reported_model,
            result=result,
            duration_ms=duration_ms,
            client_name=self.client_name,
            client_version=self.client_version,
            protocol_version=self.protocol_version,
            conversation_id=conversation_id,
            extra=self.extra,
        )


def start_tool_call_lifecycle(
    data: MCPAnalyticsData,
    *,
    name: str,
    arguments: Optional[Dict[str, Any]],
    request_meta: Optional[Dict[str, Any]],
    allow_self_reported_model: bool,
    mcp_session_id: Optional[str],
    token: Optional[SessionTokenPayload],
    client_name: Optional[str],
    client_version: Optional[str],
    protocol_version: Optional[str],
    extra: Dict[str, Any],
) -> ToolCallLifecycle:
    """Resolve adapter-independent policy for a tool call without dispatching it."""
    enabled = enabled_virtual_tool_names(data)
    missing_name = enabled.get(VIRTUAL_TOOL_MISSING_CAPABILITY)
    feedback_name = enabled.get(VIRTUAL_TOOL_FEEDBACK)
    # Still needed whatever the name resolution says: parsing the report and
    # running the host's `on_feedback` handler read the configured options.
    feedback_options = resolve_collect_feedback_options(data.options.collect_feedback)
    conversation_id, minted = resolve_conversation_id(
        data.options.enable_conversation_id, arguments
    )
    # A carried session stays stable until the agent supplies its own handle.
    has_carried_session = token is not None or bool(mcp_session_id)
    if minted and has_carried_session:
        conversation_id, minted = None, False
    return ToolCallLifecycle(
        data=data,
        name=name,
        arguments=arguments,
        request_meta=request_meta,
        allow_self_reported_model=allow_self_reported_model,
        request=build_tool_call_request(name, arguments),
        extra=extra,
        mcp_session_id=mcp_session_id,
        token=token,
        client_name=client_name,
        client_version=client_version,
        protocol_version=protocol_version,
        missing_name=missing_name,
        feedback_options=feedback_options,
        feedback_name=feedback_name,
        conversation_id=conversation_id,
        minted_conversation_id=minted,
    )


async def record_tool_call(
    data: MCPAnalyticsData,
    session_id: str,
    *,
    name: str,
    arguments: Optional[Dict[str, Any]],
    request_meta: Optional[Dict[str, Any]] = None,
    allow_self_reported_model: bool = False,
    result: Any = None,
    error: Any = None,
    duration_ms: Optional[float] = None,
    client_name: Optional[str] = None,
    client_version: Optional[str] = None,
    protocol_version: Optional[str] = None,
    conversation_id: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    # Analytics must never change what the tool returns or raises: any failure
    # building/publishing the event is logged and swallowed here.
    try:
        request = build_tool_call_request(name, arguments)
        event: Dict[str, Any] = {
            "event_type": MCPAnalyticsEventType.MCP_TOOLS_CALL,
            "session_id": session_id,
            "resource_name": name,
            "tool_description": data.tool_descriptions.get(name),
            "tool_category": data.tool_categories.get(name),
            "parameters": build_captured_mcp_parameters(
                request, strip_llm_model=allow_self_reported_model
            ),
            "duration": duration_ms,
            "client_name": client_name,
            "client_version": client_version,
            "protocol_version": protocol_version,
            "conversation_id": conversation_id,
            "is_error": False,
        }
        set_event_intent(event, await resolve_tool_call_intent(data, request, extra))
        if is_capture_model_enabled(data.options.capture_model):
            model, source = resolve_model(
                request_meta,
                arguments,
                allow_self_reported=allow_self_reported_model,
            )
            if model:
                event["llm_model"] = model
                event["llm_model_source"] = source

        if error is not None:
            event["is_error"] = True
            event["error"] = capture_exception(error)
        elif result is not None:
            event["response"] = _wrap_response(result)
            if is_tool_result_error(result):
                event["is_error"] = True
                event["error"] = capture_exception(result)

        props = await resolve_event_properties(data, request, extra)
        if props is not None:
            event["properties"] = props

        stamp_transport_identity(event, extra)
        fire_and_forget(capture_event(data, event), data)
    except Exception as err:  # noqa: BLE001 - isolate analytics from the tool path
        log(f"record_tool_call failed (event dropped, tool unaffected): {err}")


def extract_tools(result: Any) -> list:
    """Pull the tool list out of a ListTools ServerResult, as a copy."""
    root = getattr(result, "root", result)
    return list(getattr(root, "tools", []) or [])


def append_virtual_tool(result: Any, tool: Any) -> Any:
    """Return a copy of a ``tools/list`` result with ``tool`` added.

    A copy, not an in-place append: a host may return the *same* result object
    from every ``tools/list``, and mutating it leaves PostHog's tool sitting in
    what later reads back as the host's own catalogue.

    1.x wraps ``ListToolsResult`` in a ``ServerResult`` root model, 2.x returns
    it directly. The copy carries every other field, ``nextCursor`` included."""
    root = getattr(result, "root", result)
    tools_list = getattr(root, "tools", None)
    if not isinstance(tools_list, list):
        return result
    updated = root.model_copy(update={"tools": [*tools_list, tool]})
    return type(result)(updated) if hasattr(result, "root") else updated


def virtual_tool_descriptor(
    data: MCPAnalyticsData, kind: str, name: str
) -> Dict[str, Any]:
    """The advertised descriptor for a virtual tool, under its configured name."""
    from .tools import build_report_missing_descriptor

    if kind == VIRTUAL_TOOL_MISSING_CAPABILITY:
        return build_report_missing_descriptor(name)
    return get_feedback_tool_descriptor(
        resolve_collect_feedback_options(data.options.collect_feedback)
    )


def append_virtual_tool_by_kind(
    result: Any, kind: str, name: str, data: MCPAnalyticsData, *, schema_field: str
) -> Any:
    """Add a virtual tool to a ``tools/list`` result and return the result to
    serve. ``schema_field`` is the SDK major's spelling of the input schema
    field: ``inputSchema`` on 1.x, ``input_schema`` on 2.x.

    ``name`` is passed in rather than re-resolved, so a rename can't drift
    between :func:`resolve_virtual_tool_injection`'s decision and this append.

    ``owns_context=True``: a virtual tool carries its intent in its own
    arguments, so no ``context`` is injected — but the capture_model pass still
    runs, so it advertises ``llm_model``."""
    import mcp.types as mcp_types

    descriptor = virtual_tool_descriptor(data, kind, name)
    tool = mcp_types.Tool(
        name=name,
        description=descriptor["description"],
        annotations=descriptor["annotations"],
        **{schema_field: descriptor["inputSchema"]},
    )
    mutate_tool_schema(
        data,
        tool,
        schema_attribute=schema_field,
        owns_context=True,
        context_required=True,
        is_sdk_virtual_tool=True,
    )
    return append_virtual_tool(result, tool)


def apply_virtual_tool_injection(
    result: Any,
    injection: Dict[str, str],
    names: List[str],
    data: MCPAnalyticsData,
    *,
    schema_field: str,
) -> Any:
    """Append every virtual tool this page won, recording each name for the
    ``$mcp_tools_list`` event. Missing-capability first: every call path tests
    that kind first, which is what makes the ``duplicate`` rule in
    :func:`resolve_virtual_tool_injection` resolve in its favour."""
    for kind in (VIRTUAL_TOOL_MISSING_CAPABILITY, VIRTUAL_TOOL_FEEDBACK):
        name = injection.get(kind)
        if name is None:
            continue
        result = append_virtual_tool_by_kind(
            result, kind, name, data, schema_field=schema_field
        )
        names.append(name)
    return result


def enabled_virtual_tool_names(data: MCPAnalyticsData) -> Dict[str, str]:
    """``{kind: configured name}`` for each virtual tool the options enable,
    ignoring collisions. The only place the two enable switches and the two
    rename options are read together, so a rename can't drift."""
    names: Dict[str, str] = {}
    if data.options.report_missing:
        names[VIRTUAL_TOOL_MISSING_CAPABILITY] = resolve_missing_capability_tool_name(
            data.options
        )
    feedback_options = resolve_collect_feedback_options(data.options.collect_feedback)
    if feedback_options is not None:
        names[VIRTUAL_TOOL_FEEDBACK] = resolve_send_feedback_tool_name(feedback_options)
    return names


def is_first_listing_page(params: Any) -> bool:
    """Whether a ``tools/list`` *request* is the first page of the listing.

    Per the MCP spec an absent ``cursor`` means "start of the listing". A present
    one -- *including the empty string* -- is an opaque value from a previous
    page, so it is a continuation. Takes the request params, so both handler
    shapes share the rule."""
    return getattr(params, "cursor", None) is None


def advertised_tool_names(tools: list) -> Set[str]:
    """The names a listing page advertises. The virtual tools are appended to a
    *copy* of the host's result, so a page always reads back as the host wrote
    it -- see :func:`append_virtual_tool`."""
    return {
        name for tool in tools if isinstance(name := getattr(tool, "name", None), str)
    }


def resolve_virtual_tool_injection(
    data: MCPAnalyticsData,
    tools: list,
    *,
    is_first_page: bool,
) -> Dict[str, str]:
    """``{kind: name}`` for each virtual tool this listing page appends. A kind
    is absent when the feature is off, a real tool owns the name, this is a
    continuation page, or the other virtual tool already claimed the name.

    Run after ``collect_listed_tools``, so the virtual tools don't count towards
    "this server advertises nothing".

    Stateless: this page's own tools are the whole input. Injection happens on a
    first page only, so only a first page's view of the tool set decides
    anything.

    * name free on the **first** page -> inject it.
    * name taken on the **first** page -> warn, inject nothing. The call path
      dispatches it normally and the host's tool wins.
    * name taken on a **later** page -> the virtual tool is already advertised
      from page one, so the host's tool is shadowed. Warn, naming the rename
      option; page one cannot be taken back.
    """
    enabled = enabled_virtual_tool_names(data)
    if not enabled:
        return {}

    listed = advertised_tool_names(tools)

    if not is_first_page:
        for kind, name in enabled.items():
            # Only warn about a tool we actually shadowed. If this kind never
            # made it onto the first page -- a real tool already held the name
            # ("blocked"), or the other virtual tool won it ("duplicate") --
            # then nothing of ours is advertised under it and the host's tool
            # runs untouched. Telling them otherwise sends them chasing a bug
            # that isn't there.
            if name in listed and not any(
                (kind, name, variant) in data.warned_virtual_tool_collisions
                for variant in ("blocked", "duplicate")
            ):
                _warn_virtual_tool_collision(data, kind, name, "shadowed")
        return {}

    injectable: Dict[str, str] = {}
    for kind, name in enabled.items():
        if name in listed:
            _warn_virtual_tool_collision(data, kind, name, "blocked")
        else:
            injectable[kind] = name

    # Both virtual tools configured with one name would advertise it twice and
    # dead-letter the feedback path, since every call path checks
    # missing-capability first. Keep that precedence and say so.
    missing_name = injectable.get(VIRTUAL_TOOL_MISSING_CAPABILITY)
    if (
        missing_name is not None
        and injectable.get(VIRTUAL_TOOL_FEEDBACK) == missing_name
    ):
        injectable.pop(VIRTUAL_TOOL_FEEDBACK)
        _warn_virtual_tool_collision(
            data, VIRTUAL_TOOL_FEEDBACK, missing_name, "duplicate"
        )

    return injectable


async def raw_listing_owns_tool_name(
    data: MCPAnalyticsData, name: str, ctx: Any = None
) -> Optional[bool]:
    """Whether the host's *own* ``tools/list`` handler advertises ``name`` on its
    first page, asked at call time. For the raw low-level paths, 1.x and v2,
    which have no tool registry to query instead.

    Tri-state. ``True``/``False`` are answers; ``None`` means the question could
    not be asked, and callers must not intercept on it -- guessing "not owned"
    would swallow a real tool of the host's. Runs once per call to a virtual
    tool's name, never for ordinary traffic.
    """
    probe = data.raw_tool_names_probe
    if probe is None:
        return None
    try:
        names = await probe(ctx)
    except Exception as err:  # noqa: BLE001 - analytics must not break the call
        warn_ownership_lookup_failed(name, err)
        return None
    return None if names is None else name in names


def warn_ownership_lookup_failed(name: str, err: Exception) -> None:
    """A tool-ownership lookup raised instead of answering. Every adapter's probe
    reports it the same way and then returns ``None``, so the call is delegated
    to the host rather than intercepted on a guess."""
    log(
        f'Warning: could not determine whether "{name}" is a real tool of '
        f"yours; delegating the call to your server - {err}"
    )


def _warn_virtual_tool_collision(
    data: MCPAnalyticsData,
    kind: str,
    name: str,
    variant: VirtualToolCollisionVariant,
) -> None:
    """Warn once per ``(kind, name, variant)`` for the life of the server's
    tracking state, so a client that re-lists tools on every turn doesn't flood
    the log with the same misconfiguration."""
    key = (kind, name, variant)
    if key in data.warned_virtual_tool_collisions:
        return
    data.warned_virtual_tool_collisions.add(key)
    warn(virtual_tool_collision_message(kind, name, variant))


def virtual_tool_collision_message(
    kind: str,
    name: str,
    variant: VirtualToolCollisionVariant,
    *,
    rename_option: Optional[str] = None,
) -> str:
    """The warning text for a virtual-tool name collision. Always names the option
    that renames PostHog's tool -- a warning without its own remedy gets ignored.
    ``rename_option`` overrides the ``instrument()`` spelling for hosts on the
    ``PostHogMCP`` dispatcher path."""
    remedy = rename_option or _VIRTUAL_TOOL_RENAME_OPTION[kind]
    if variant == "shadowed":
        return (
            f'Warning: a later tools/list page advertises a real tool named "{name}", '
            "but PostHog already injected its own tool by that name on the first page. "
            f'Calls to "{name}" are intercepted by PostHog and the real tool will not '
            f"run. Rename one of them; {remedy} renames PostHog's."
        )
    event = _VIRTUAL_TOOL_EVENT[kind]
    if variant == "duplicate":
        return (
            "Warning: PostHog's missing-capability and agent-feedback tools are both "
            f'configured to use the name "{name}". Only the missing-capability tool is '
            f"advertised and intercepted, so no {event} events will be captured. "
            f"Rename one with {remedy}."
        )
    return (
        f'Warning: Cannot inject PostHog\'s "{name}" tool because a real tool already '
        f"uses that name. PostHog will not intercept it and no {event} events will be "
        f"captured. Rename PostHog's tool with {remedy}."
    )


def read_tool_category(tool: Any) -> Optional[str]:
    """Read a tool's product category from its ``_meta.category``."""
    meta = getattr(tool, "meta", None)
    if isinstance(meta, dict):
        category = meta.get("category")
        if isinstance(category, str):
            return category
    return None


def collect_listed_tools(data: MCPAnalyticsData, tools: list) -> tuple[List[str], bool]:
    """Cache common tool metadata and return the pre-injection listing summary."""
    names = []
    for tool in tools:
        names.append(tool.name)
        if getattr(tool, "description", None):
            data.tool_descriptions[tool.name] = tool.description
        category = read_tool_category(tool)
        if category:
            data.tool_categories[tool.name] = category
    return names, not tools


def mutate_tool_schema(
    data: MCPAnalyticsData,
    tool: Any,
    *,
    schema_attribute: str,
    owns_context: bool,
    context_required: bool,
    is_sdk_virtual_tool: bool,
) -> None:
    """Apply the common analytics schema pipeline and write it back in place.

    The adapter explicitly supplies its SDK model's schema attribute and its own
    ownership decision. Those are the parts that differ across MCP generations;
    context/conversation mutation and output-channel bookkeeping do not.
    """
    schema = getattr(tool, schema_attribute, None)
    original_schema = schema
    if (
        not is_sdk_virtual_tool
        and is_context_enabled(data.options.context)
        and not owns_context
    ):
        schema = add_context_parameter_to_schema(
            schema,
            tool.name,
            get_context_description(data.options.context),
            required=context_required,
        )
    if is_capture_model_enabled(data.options.capture_model):
        model_was_injected = data.tool_model_parameter_injected.get(tool.name, False)
        app_owns_model = (
            schema_has_param(schema, "llm_model") and not model_was_injected
        )
        if not app_owns_model and not schema_has_param(schema, "llm_model"):
            schema = add_model_parameter_to_schema(
                schema,
                tool.name,
                get_model_description(data.options.capture_model),
                required=context_required,
            )
        data.tool_model_parameter_injected[tool.name] = (
            not app_owns_model and schema_has_param(schema, "llm_model")
        )
    if data.options.enable_conversation_id and not schema_has_param(
        schema, "conversation_id"
    ):
        schema = add_conversation_id_to_schema(schema, tool.name)
    if schema is not original_schema:
        try:
            setattr(tool, schema_attribute, schema)
        except Exception:  # noqa: BLE001 - some schema attrs may be read-only
            log(f"WARN: could not set {schema_attribute} on tool {tool.name}")
    if data.options.enable_conversation_id:
        data.tool_output_instructions[tool.name] = add_instructions_to_output_schema(
            tool
        )


def request_to_dict(req: Any) -> Dict[str, Any]:
    """Shape a request object into the JSON-RPC-ish dict the sanitizer expects."""
    method = getattr(req, "method", None) or "tools/list"
    params = getattr(req, "params", None)
    return params_to_request_dict(method, params)


def params_to_request_dict(
    method: str, params: Any, *, by_alias: bool = False
) -> Dict[str, Any]:
    """Shape a bare ``(method, params)`` pair into the same JSON-RPC-ish dict
    ``request_to_dict`` builds from a request object. v2's request handlers
    receive ``params`` directly rather than a ``req`` wrapper, so there's no
    object to hand ``request_to_dict``; ``by_alias`` lets v2 keep the wire's
    camelCase aliases (its models expose snake_case attributes)."""
    params_dict: Any = {}
    if params is not None and hasattr(params, "model_dump"):
        try:
            params_dict = params.model_dump(mode="json", by_alias=by_alias)
        except Exception:  # noqa: BLE001
            params_dict = {}
    return {"method": method, "params": params_dict}


@dataclass(frozen=True)
class ToolsListLifecycle:
    """Common capture lifecycle for one client-facing tools/list dispatch."""

    data: MCPAnalyticsData
    session_id: str
    request: Dict[str, Any]
    extra: Dict[str, Any]
    client_name: Optional[str]
    client_version: Optional[str]
    protocol_version: Optional[str]

    async def record_error(self, error: Any, duration_ms: float) -> None:
        await record_tools_list(
            self.data,
            self.session_id,
            names=[],
            request=self.request,
            duration_ms=duration_ms,
            is_error=True,
            error=error,
            client_name=self.client_name,
            client_version=self.client_version,
            protocol_version=self.protocol_version,
            extra=self.extra,
        )

    async def record_result(
        self,
        *,
        names: List[str],
        response: Any,
        duration_ms: float,
        is_empty: bool,
    ) -> None:
        await record_tools_list(
            self.data,
            self.session_id,
            names=names,
            request=self.request,
            response=response,
            duration_ms=duration_ms,
            is_error=is_empty,
            error="tools/list returned no tools" if is_empty else None,
            client_name=self.client_name,
            client_version=self.client_version,
            protocol_version=self.protocol_version,
            extra=self.extra,
        )


async def start_tools_list_lifecycle(
    data: MCPAnalyticsData,
    *,
    request: Dict[str, Any],
    extra: Dict[str, Any],
    mcp_session_id: Optional[str],
    token: Optional[SessionTokenPayload],
    client_name: Optional[str],
    client_version: Optional[str],
    protocol_version: Optional[str],
) -> ToolsListLifecycle:
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
    return ToolsListLifecycle(
        data=data,
        session_id=session_id,
        request=request,
        extra=extra,
        client_name=client_name,
        client_version=client_version,
        protocol_version=protocol_version,
    )


async def record_missing_capability(
    data: MCPAnalyticsData,
    session_id: str,
    *,
    conversation_id: Optional[str] = None,
    tool_name: str,
    context: Optional[str],
    arguments: Optional[Dict[str, Any]],
    request_meta: Optional[Dict[str, Any]] = None,
    allow_self_reported_model: bool = False,
    client_name: Optional[str] = None,
    client_version: Optional[str] = None,
    protocol_version: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Record a ``get_more_tools`` call as ``$mcp_missing_capability``, with the
    agent's stated need as ``$mcp_intent``."""
    try:
        request = build_tool_call_request(tool_name, arguments)
        event: Dict[str, Any] = {
            "event_type": MCPAnalyticsEventType.MCP_MISSING_CAPABILITY,
            "session_id": session_id,
            "conversation_id": conversation_id,
            "resource_name": tool_name,
            "parameters": build_captured_mcp_parameters(
                request, strip_llm_model=allow_self_reported_model
            ),
            "client_name": client_name,
            "client_version": client_version,
            "protocol_version": protocol_version,
        }
        if isinstance(context, str) and context.strip():
            event["user_intent"] = context.strip()
            event["user_intent_source"] = "context_parameter"
        if is_capture_model_enabled(data.options.capture_model):
            model, source = resolve_model(
                request_meta,
                arguments,
                allow_self_reported=allow_self_reported_model,
            )
            if model:
                event["llm_model"] = model
                event["llm_model_source"] = source
        await _apply_event_properties(data, event, request, extra)
        stamp_transport_identity(event, extra)
        fire_and_forget(capture_event(data, event), data)
    except Exception as err:  # noqa: BLE001 - isolate analytics from the tool path
        log(f"record_missing_capability failed (event dropped): {err}")


async def record_feedback(
    data: MCPAnalyticsData,
    session_id: str,
    *,
    conversation_id: Optional[str] = None,
    report: FeedbackReport,
    tool_name: str,
    arguments: Optional[Dict[str, Any]],
    request_meta: Optional[Dict[str, Any]] = None,
    client_name: Optional[str] = None,
    client_version: Optional[str] = None,
    protocol_version: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Record a ``send_feedback`` call as ``$mcp_feedback``, with the report's
    summary and details as ``$mcp_intent``.

    Deliberately no ``parameters``: the arguments are agent-narrated free text,
    and the PII-redacted ``$mcp_feedback_*`` properties are the captured surface —
    the raw arguments would bypass that redaction and record undeclared fields."""
    try:
        request = build_tool_call_request(tool_name, arguments)
        event: Dict[str, Any] = {
            "event_type": MCPAnalyticsEventType.MCP_FEEDBACK,
            "session_id": session_id,
            "conversation_id": conversation_id,
            "resource_name": tool_name,
            "client_name": client_name,
            "client_version": client_version,
            "protocol_version": protocol_version,
        }
        intent = build_feedback_intent(report)
        if intent:
            event["user_intent"] = intent
            event["user_intent_source"] = "context_parameter"
        if is_capture_model_enabled(data.options.capture_model):
            model, source = resolve_model(
                request_meta, arguments, allow_self_reported=True
            )
            if model:
                event["llm_model"] = model
                event["llm_model_source"] = source
        # Merged by hand (not `_apply_event_properties`, which assigns) so the
        # customer's event_properties callback can't clobber the feedback fields.
        props = await resolve_event_properties(data, request, extra)
        event["properties"] = {
            **(props or {}),
            **build_feedback_event_properties(report),
        }
        stamp_transport_identity(event, extra)
        fire_and_forget(capture_event(data, event), data)
    except Exception as err:  # noqa: BLE001 - isolate analytics from the tool path
        log(f"record_feedback failed (event dropped): {err}")


async def record_tools_list(
    data: MCPAnalyticsData,
    session_id: str,
    *,
    names: List[str],
    request: Dict[str, Any],
    response: Any = None,
    duration_ms: Optional[float] = None,
    is_error: bool = False,
    error: Any = None,
    client_name: Optional[str] = None,
    client_version: Optional[str] = None,
    protocol_version: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        event: Dict[str, Any] = {
            "event_type": MCPAnalyticsEventType.MCP_TOOLS_LIST,
            "session_id": session_id,
            "listed_tool_names": names,
            "parameters": build_captured_mcp_parameters(request),
            "response": _wrap_response(response) if response is not None else None,
            "duration": duration_ms,
            "client_name": client_name,
            "client_version": client_version,
            "protocol_version": protocol_version,
            "is_error": is_error,
            "timestamp": datetime.now(timezone.utc),
        }
        if error is not None:
            event["error"] = capture_exception(error)
        await _apply_event_properties(data, event, request, extra)
        stamp_transport_identity(event, extra)
        fire_and_forget(capture_event(data, event), data)
    except Exception as err:  # noqa: BLE001 - isolate analytics from the tool path
        log(f"record_tools_list failed (event dropped): {err}")


def resource_listing_response(event_type: str, result: Any) -> Any:
    """The result an adapter should capture as the event ``response``. A listing
    (``resources/list``, ``resources/templates/list``) is metadata — names, uris,
    mime types — so it is captured; a read's result is the resource body itself,
    which this SDK never captures."""
    if event_type != MCPAnalyticsEventType.MCP_RESOURCES_LIST:
        return None
    return _to_jsonable(result)


async def record_resource_request(
    data: MCPAnalyticsData,
    session_id: str,
    *,
    event_type: str,
    request: Dict[str, Any],
    response: Any = None,
    error: Any = None,
    duration_ms: Optional[float] = None,
    client_name: Optional[str] = None,
    client_version: Optional[str] = None,
    protocol_version: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Record a resources listing or read without affecting dispatch."""
    try:
        params = request.get("params")
        uri = params.get("uri") if isinstance(params, dict) else None
        event: Dict[str, Any] = {
            "event_type": event_type,
            "session_id": session_id,
            "resource_name": uri
            if event_type == MCPAnalyticsEventType.MCP_RESOURCES_READ
            else None,
            "parameters": build_captured_mcp_parameters(request),
            "response": _wrap_response(response) if response is not None else None,
            "duration": duration_ms,
            "client_name": client_name,
            "client_version": client_version,
            "protocol_version": protocol_version,
            "is_error": error is not None,
            "timestamp": datetime.now(timezone.utc),
        }
        if error is not None:
            event["error"] = capture_exception(error)
        await _apply_event_properties(data, event, request, extra)
        stamp_transport_identity(event, extra)
        fire_and_forget(capture_event(data, event), data)
    except Exception as err:  # noqa: BLE001 - isolate analytics from the request path
        log(f"record_resource_request failed (event dropped): {err}")
