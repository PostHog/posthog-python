# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Shared types for the MCP analytics SDK.

The internal ``Event``/``McpEvent`` is modeled as a ``dict`` (typed via
``TypedDict``, ``total=False``) to faithfully mirror the TypeScript SDK's plain
objects: the pipeline shallow-copies with ``{**event}``, reads fields with
``.get()``, and JSON-serializes the whole event for byte-size budgeting. Keys
are snake_case internally; ``posthog_events`` maps them to the ``$mcp_*`` wire
keys. Public option/identity shapes (added with the server adapters) are
dataclasses for a nicer API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

try:
    from typing import TypedDict
except ImportError:  # pragma: no cover - 3.10 has TypedDict in typing
    from typing_extensions import TypedDict  # type: ignore

JsonRecord = Dict[str, Any]

# PostHog error-tracking properties (the ``$exception_list`` / ``$exception_level`` shape).
ErrorProperties = Dict[str, Any]

MCPAnalyticsIntentSource = str  # "context_parameter" | "inferred"


class Event(TypedDict, total=False):
    """Internal MCP event as it flows through the SDK before capture.

    ``McpEvent`` is the same shape — every field is optional (``total=False``),
    so a partially-built event and a complete one share the type.
    """

    actor_id: str
    client_name: str
    client_version: str
    conversation_id: str
    duration: float
    error: Optional[ErrorProperties]
    event_id: str
    event_type: str
    groups: Dict[str, str]
    # Explicit PostHog event name. When set (via the custom-event handle) it
    # overrides the built-in name derived from event_type, and is sent verbatim.
    event_name: str
    id: str
    identify_actor_data: JsonRecord
    identify_actor_given_id: str
    ip_address: str
    is_error: bool
    listed_tool_names: List[str]
    parameters: Any
    properties: Optional[JsonRecord]
    resource_name: str
    response: Any
    sdk_language: str
    sdk_version: str
    server_name: str
    server_version: str
    session_id: str
    timestamp: datetime
    tool_category: str
    tool_description: str
    user_intent: str
    user_intent_source: str


# McpEvent is an alias — total=False already makes Event a "partial".
McpEvent = Event


class PostHogCaptureEvent(TypedDict, total=False):
    """A fully-built payload ready for ``Client.capture()``."""

    distinct_id: str
    event: str
    properties: Dict[str, Any]
    timestamp: datetime


# Hook invoked for every event just before capture. Return the (possibly
# mutated) event to send it, or a nullish value to drop it. May be sync or async.
BeforeSendFn = Callable[
    [PostHogCaptureEvent],
    Union[Optional[PostHogCaptureEvent], Awaitable[Optional[PostHogCaptureEvent]]],
]
