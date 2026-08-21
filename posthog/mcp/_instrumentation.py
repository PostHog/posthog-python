# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Shared tool-call / tools-list / initialize lifecycle used by both the FastMCP
and low-level server adapters. The adapters resolve transport-specific details
(client info, session id, raw result shape) and delegate the analytics flow here
so both stay in sync."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ._capture import capture_event
from ._event_types import MCPAnalyticsEventType
from ._exceptions import capture_exception
from ._intent import resolve_tool_call_intent, set_event_intent
from ._internal import MCPAnalyticsData, handle_identify, resolve_event_properties
from .logger import log
from ._sanitization import build_captured_mcp_parameters
from ._transport_identity import stamp_transport_identity
from .session import resolve_session_id
from .session_token import SessionTokenPayload, decode_session_id

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
    return (
        getattr(result, "isError", None) is True
        or getattr(result, "is_error", None) is True
    )


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
    the Python SDK handles initialize in the session layer, not ``request_handlers``.)"""
    session_id = await resolve_session_id(
        data, mcp_session_id, token=token, conversation_id=conversation_id
    )
    identify_event = await handle_identify(data, session_id, request, extra)
    if identify_event:
        fire_and_forget(capture_event(data, identify_event), data)
    await _maybe_emit_initialize(
        data, session_id, client_name, client_version, extra, protocol_version
    )
    return session_id


async def record_tool_call(
    data: MCPAnalyticsData,
    session_id: str,
    *,
    name: str,
    arguments: Optional[Dict[str, Any]],
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
            "parameters": build_captured_mcp_parameters(request),
            "duration": duration_ms,
            "client_name": client_name,
            "client_version": client_version,
            "protocol_version": protocol_version,
            "conversation_id": conversation_id,
            "is_error": False,
        }
        set_event_intent(event, await resolve_tool_call_intent(data, request, extra))

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
    """Pull the tool list out of a ListTools ServerResult (a copy — to MUTATE the
    real list use ``append_get_more_tools``)."""
    root = getattr(result, "root", result)
    return list(getattr(root, "tools", []) or [])


def append_get_more_tools(result: Any, name: str) -> None:
    """Append the get_more_tools virtual tool to the real ListToolsResult.tools list."""
    import mcp.types as mcp_types

    from .tools import build_report_missing_descriptor

    descriptor = build_report_missing_descriptor(name)
    tool = mcp_types.Tool(
        name=descriptor["name"],
        description=descriptor["description"],
        inputSchema=descriptor["inputSchema"],
        annotations=descriptor["annotations"],
    )
    root = getattr(result, "root", result)
    tools_list = getattr(root, "tools", None)
    if isinstance(tools_list, list):
        tools_list.append(tool)


def read_tool_category(tool: Any) -> Optional[str]:
    """Read a tool's product category from its ``_meta.category``."""
    meta = getattr(tool, "meta", None)
    if isinstance(meta, dict):
        category = meta.get("category")
        if isinstance(category, str):
            return category
    return None


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


async def record_missing_capability(
    data: MCPAnalyticsData,
    session_id: str,
    *,
    tool_name: str,
    context: Optional[str],
    arguments: Optional[Dict[str, Any]],
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
            "resource_name": tool_name,
            "parameters": build_captured_mcp_parameters(request),
            "client_name": client_name,
            "client_version": client_version,
            "protocol_version": protocol_version,
        }
        if isinstance(context, str) and context.strip():
            event["user_intent"] = context.strip()
            event["user_intent_source"] = "context_parameter"
        await _apply_event_properties(data, event, request, extra)
        stamp_transport_identity(event, extra)
        fire_and_forget(capture_event(data, event), data)
    except Exception as err:  # noqa: BLE001 - isolate analytics from the tool path
        log(f"record_missing_capability failed (event dropped): {err}")


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
