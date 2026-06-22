# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Shared tool-call / tools-list / initialize lifecycle used by both the FastMCP
and low-level server adapters. The adapters resolve transport-specific details
(client info, session id, raw result shape) and delegate the analytics flow here
so both stay in sync."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from .capture import capture_event
from .event_types import MCPAnalyticsEventType
from .exceptions import capture_exception
from .intent import resolve_tool_call_intent, set_event_intent
from .internal import MCPAnalyticsData, handle_identify, resolve_event_properties
from .sanitization import build_captured_mcp_parameters
from .session import resolve_session_id

# Keep strong refs to fire-and-forget capture tasks so they aren't GC'd mid-flight.
_BACKGROUND_TASKS: Set[asyncio.Task] = set()


def fire_and_forget(coro: Optional[Any]) -> None:
    """Schedule a capture coroutine without blocking the tool path. No-ops if the
    coroutine is ``None`` (no sink) or there is no running event loop."""
    if coro is None:
        return
    try:
        task = asyncio.ensure_future(coro)
    except RuntimeError:
        # No running loop — run it to completion synchronously as a fallback.
        asyncio.new_event_loop().run_until_complete(coro)
        return
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


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
) -> None:
    """Lazily emit ``$mcp_initialize`` once per session. The Python MCP SDK handles
    ``InitializeRequest`` inside the session layer (not ``request_handlers``), so we
    synthesize the event from the first instrumented request that carries client info."""
    if session_id in data.initialized_sessions:
        return
    data.initialized_sessions.add(session_id)
    fire_and_forget(
        capture_event(
            data,
            {
                "event_type": MCPAnalyticsEventType.MCP_INITIALIZE,
                "session_id": session_id,
                "client_name": client_name,
                "client_version": client_version,
                "timestamp": datetime.now(timezone.utc),
            },
        )
    )


async def prepare_request(
    data: MCPAnalyticsData,
    *,
    mcp_session_id: Optional[str],
    client_name: Optional[str],
    client_version: Optional[str],
    request: Dict[str, Any],
    extra: Optional[Dict[str, Any]],
) -> str:
    """Resolve the session id, lazily emit initialize, and run identify. Returns
    the session id to stamp on the event for this request."""
    session_id = await resolve_session_id(data, mcp_session_id)
    await _maybe_emit_initialize(data, session_id, client_name, client_version)
    identify_event = await handle_identify(data, session_id, request, extra)
    if identify_event:
        fire_and_forget(capture_event(data, identify_event))
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
    extra: Optional[Dict[str, Any]] = None,
) -> None:
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


def record_tools_list(
    data: MCPAnalyticsData,
    session_id: str,
    *,
    names: List[str],
    request: Dict[str, Any],
    client_name: Optional[str] = None,
    client_version: Optional[str] = None,
) -> None:
    fire_and_forget(
        capture_event(
            data,
            {
                "event_type": MCPAnalyticsEventType.MCP_TOOLS_LIST,
                "session_id": session_id,
                "listed_tool_names": names,
                "parameters": build_captured_mcp_parameters(request),
                "client_name": client_name,
                "client_version": client_version,
                "timestamp": datetime.now(timezone.utc),
            },
        )
    )
