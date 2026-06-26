# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Materialize an ``McpEvent`` against per-server tracking data + resolved
identity, then hand it to the ``McpEventSink`` for the
sanitize/truncate/before_send/capture pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Coroutine, Dict, Optional

from ._event_types import MCPAnalyticsEventType
from ._internal import MCPAnalyticsData
from .logger import log
from ._sink import McpCaptureOptions
from .version import __version__


def capture_event(
    data: MCPAnalyticsData, event_input: Dict[str, Any]
) -> Optional[Coroutine[Any, Any, None]]:
    """Enrich an event with session/identity/server/sdk metadata and return the
    sink's capture coroutine (so the custom-event handle can await it). Auto-capture
    callers schedule it and ignore the result. Returns ``None`` if no sink is attached."""
    sink = data.sink
    if sink is None:
        return None

    session_id = event_input.get("session_id") or data.session_id
    actor = data.identified_sessions.get(session_id)

    timestamp = event_input.get("timestamp") or datetime.now(timezone.utc)
    duration = event_input.get("duration")
    if duration is None and event_input.get("timestamp"):
        duration = (datetime.now(timezone.utc) - timestamp).total_seconds() * 1000

    full_event: Dict[str, Any] = {
        "id": event_input.get("id") or "",
        "session_id": session_id,
        "event_type": event_input.get("event_type") or MCPAnalyticsEventType.CUSTOM,
        "event_name": event_input.get("event_name"),
        "timestamp": timestamp,
        "duration": duration,
        "sdk_language": "Python",
        "sdk_version": __version__,
        "server_name": data.server_name,
        "server_version": data.server_version,
        "client_name": event_input.get("client_name"),
        "client_version": event_input.get("client_version"),
        "identify_actor_given_id": actor.distinct_id if actor else None,
        "identify_actor_data": (actor.properties or {}) if actor else {},
        "groups": actor.groups if actor else None,
        "resource_name": event_input.get("resource_name"),
        "tool_category": event_input.get("tool_category"),
        "tool_description": event_input.get("tool_description"),
        "listed_tool_names": event_input.get("listed_tool_names"),
        "parameters": event_input.get("parameters"),
        "response": event_input.get("response"),
        "user_intent": event_input.get("user_intent"),
        "user_intent_source": event_input.get("user_intent_source"),
        "is_error": event_input.get("is_error"),
        "error": event_input.get("error"),
        "conversation_id": event_input.get("conversation_id"),
        "properties": event_input.get("properties"),
    }

    options = McpCaptureOptions(
        enable_exception_autocapture=data.options.enable_exception_autocapture,
        before_send=data.options.before_send,
    )
    return sink.capture(full_event, options)


def log_capture_skipped() -> None:
    log("Warning: Server tracking data not found. Event will not be published.")
