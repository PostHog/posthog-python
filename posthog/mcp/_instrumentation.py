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
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from ._capture import capture_event
from ._event_types import MCPAnalyticsEventType
from ._exceptions import capture_exception
from ._intent import resolve_tool_call_intent, set_event_intent
from ._internal import MCPAnalyticsData, handle_identify, resolve_event_properties
from .logger import log
from ._sanitization import build_captured_mcp_parameters
from .session import resolve_session_id
from .session_token import SessionTokenPayload, decode_session_id

# Keep strong refs to in-flight capture tasks/futures so they aren't GC'd mid-flight,
# and so the asyncio ones can be awaited via drain_pending() before shutdown. Holds
# asyncio.Task (running-loop path) or concurrent.futures.Future (sync background-loop path).
_BACKGROUND_TASKS: Set[Any] = set()

# A single daemon event loop for hosts with no running loop (sync dispatchers
# like PostHogMCP). Created lazily and reused, so we never leak a loop per call.
_bg_loop: Optional[asyncio.AbstractEventLoop] = None
_bg_loop_lock = threading.Lock()


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


def _on_task_done(task: Any) -> None:
    _BACKGROUND_TASKS.discard(task)
    try:
        if not task.cancelled() and task.exception() is not None:
            log(f"background capture task failed: {task.exception()}")
    except Exception:  # noqa: BLE001 - never let bookkeeping raise
        pass


def fire_and_forget(coro: Optional[Any]) -> None:
    """Schedule a capture coroutine without blocking the tool path. No-ops if the
    coroutine is ``None`` (no sink). Runs on the current loop when there is one,
    otherwise on a shared daemon loop (sync hosts) — never creates a throwaway loop."""
    if coro is None:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (sync host) — schedule on the shared background loop.
        future = asyncio.run_coroutine_threadsafe(coro, _get_background_loop())
        _BACKGROUND_TASKS.add(future)
        future.add_done_callback(_on_task_done)
        return
    task = asyncio.ensure_future(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_on_task_done)


async def drain_pending() -> None:
    """Await in-flight capture work before ``posthog.shutdown()`` instead of racing a
    sleep. Covers both paths: ``asyncio.Task`` (running-loop hosts) and the
    ``concurrent.futures.Future`` scheduled on the background loop (sync hosts like
    PostHogMCP) — the latter wrapped so it can be awaited on the current loop."""
    awaitables: List[Any] = []
    for t in list(_BACKGROUND_TASKS):
        if isinstance(t, asyncio.Task):
            if not t.done():
                awaitables.append(t)
        elif isinstance(t, concurrent.futures.Future):
            if not t.done():
                awaitables.append(asyncio.wrap_future(t))
    if awaitables:
        await asyncio.gather(*awaitables, return_exceptions=True)


def drain_pending_sync(timeout: Optional[float] = None) -> None:
    """Block until background-loop captures finish. For sync hosts (PostHogMCP) that
    can't await :func:`drain_pending` — call it before ``flush()``/``shutdown()`` so
    trailing events aren't still in flight when the client tears down."""
    futures = [
        t
        for t in list(_BACKGROUND_TASKS)
        if isinstance(t, concurrent.futures.Future) and not t.done()
    ]
    if futures:
        concurrent.futures.wait(futures, timeout=timeout)


def is_tool_result_error(result: Any) -> bool:
    """MCP tool results signal errors via ``isError: true`` rather than raising."""
    if isinstance(result, dict):
        return result.get("isError") is True
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
        try:
            return obj.model_dump(mode="json")
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
        "timestamp": datetime.now(timezone.utc),
    }
    await _apply_event_properties(
        data, event, {"method": "initialize", "params": {}}, extra
    )
    fire_and_forget(capture_event(data, event))


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
) -> tuple[Optional[SessionTokenPayload], Optional[str], Optional[str]]:
    """Decode a replayed ``Mcp-Session-Id`` value as a self-encoded session token,
    and backfill the client name/version from it when the live transport supplied
    none (the stateless-pod case, where ``initialize`` was never seen here).

    Returns ``(token, client_name, client_version)``; ``token`` is ``None`` when the
    header isn't one of our tokens (a plain transport UUID, JWT, or nothing)."""
    token = decode_session_id(raw_session_id)
    if token is not None:
        client_name = client_name or token.client_name
        client_version = client_version or token.client_version
    return token, client_name, client_version


async def prepare_request(
    data: MCPAnalyticsData,
    *,
    mcp_session_id: Optional[str],
    client_name: Optional[str],
    client_version: Optional[str],
    request: Dict[str, Any],
    extra: Optional[Dict[str, Any]],
    token: Optional[SessionTokenPayload] = None,
) -> str:
    """Resolve the session id, run identify, then lazily emit initialize. Returns
    the session id to stamp on the event for this request.

    ``token`` is the decoded self-encoded session token (see ``session_token.py``);
    when present it takes precedence over ``mcp_session_id`` and carries the client
    identity across stateless pods.

    Identify runs *before* initialize so the resolved identity is already in the cache
    when ``capture_event`` builds the initialize event — otherwise the first
    ``$mcp_initialize`` is anonymous even when identify resolves on the same request.
    (Still not byte-parity with the TS SDK, which wraps the real initialize handler;
    the Python SDK handles initialize in the session layer, not ``request_handlers``.)"""
    session_id = await resolve_session_id(data, mcp_session_id, token=token)
    identify_event = await handle_identify(data, session_id, request, extra)
    if identify_event:
        fire_and_forget(capture_event(data, identify_event))
    await _maybe_emit_initialize(data, session_id, client_name, client_version, extra)
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

        fire_and_forget(capture_event(data, event))
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
    params_dict: Any = {}
    if params is not None and hasattr(params, "model_dump"):
        try:
            params_dict = params.model_dump(mode="json")
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
        }
        if isinstance(context, str) and context.strip():
            event["user_intent"] = context.strip()
            event["user_intent_source"] = "context_parameter"
        await _apply_event_properties(data, event, request, extra)
        fire_and_forget(capture_event(data, event))
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
            "is_error": is_error,
            "timestamp": datetime.now(timezone.utc),
        }
        if error is not None:
            event["error"] = capture_exception(error)
        await _apply_event_properties(data, event, request, extra)
        fire_and_forget(capture_event(data, event))
    except Exception as err:  # noqa: BLE001 - isolate analytics from the tool path
        log(f"record_tools_list failed (event dropped): {err}")
