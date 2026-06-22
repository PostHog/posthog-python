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

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional, TypedDict, Union

from .logger import LoggerFn

JsonRecord = Dict[str, Any]

# PostHog error-tracking properties (the ``$exception_list`` / ``$exception_level`` shape).
ErrorProperties = Dict[str, Any]

MCPAnalyticsIntentSource = str  # "context_parameter" | "inferred"

# Internal MCP event as it flows through the SDK before capture. Modeled as a
# plain dict (constructed and read with ``.get()`` throughout) to mirror the TS
# plain-object pipeline. Snake_case keys map to the ``$mcp_*`` wire keys in
# ``posthog_events``. Known keys: client_name, client_version, conversation_id,
# duration, error, event_name, event_type, groups, id, identify_actor_data,
# identify_actor_given_id, is_error, listed_tool_names, parameters, properties,
# resource_name, response, server_name, server_version, session_id, timestamp,
# tool_category, tool_description, user_intent, user_intent_source.
Event = Dict[str, Any]
McpEvent = Dict[str, Any]


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


@dataclass
class UserIdentity:
    """Resolved identity for a session. ``distinct_id`` becomes ``distinct_id``;
    ``properties`` go to ``$set``; ``groups`` (``{group_type: group_key}``) are
    stamped on every event as ``$groups``."""

    distinct_id: str
    properties: Optional[JsonRecord] = None
    groups: Optional[Dict[str, str]] = None


@dataclass
class MCPAnalyticsContextOptions:
    description: Optional[str] = None


# request is a JSON-RPC-shaped dict; extra carries session_id / headers.
IdentifyFn = Callable[
    ..., Any
]  # (request, extra) -> Optional[UserIdentity] | awaitable
IntentFallbackFn = Callable[..., Any]  # (request, extra) -> Optional[str] | awaitable
EventPropertiesFn = Callable[..., Any]  # (request, extra) -> Optional[dict] | awaitable


@dataclass
class MCPAnalyticsOptions:
    """Configuration for ``instrument()``. Mirrors the TypeScript SDK's options."""

    logger: Optional[LoggerFn] = None
    report_missing: bool = False
    missing_capability_tool_name: Optional[str] = None
    enable_conversation_id: bool = False
    enable_exception_autocapture: bool = True
    # Inject a required `context` parameter on every tool to capture user intent.
    context: Union[bool, MCPAnalyticsContextOptions] = True
    # Identify the calling user — a callable (request, extra) -> UserIdentity|None
    # (sync or async), or a static UserIdentity.
    identify: Optional[Union[IdentifyFn, UserIdentity]] = None
    # Called when a tool is invoked without an explicit `context` argument.
    intent_fallback: Optional[IntentFallbackFn] = None
    # Inspect/modify/drop each event right before it is sent to PostHog.
    before_send: Optional[BeforeSendFn] = None
    # Extra properties merged onto every auto-captured event.
    event_properties: Optional[EventPropertiesFn] = None


@dataclass
class CaptureEventData:
    """Payload for the custom-event handle returned by ``instrument()``."""

    event: str
    properties: Optional[JsonRecord] = None


@dataclass
class PreparedToolCall:
    """Result of :meth:`PostHogMCP.prepare_tool_call`: the intent pulled off the
    call, the arguments with the injected ``context`` stripped, and whether the
    call targeted the ``get_more_tools`` virtual tool."""

    args: Optional[JsonRecord] = None
    intent: Optional[str] = None
    intent_source: Optional[str] = None
    is_missing_capability: bool = False


@dataclass
class SessionInfo:
    client_name: Optional[str] = None
    client_version: Optional[str] = None
    server_name: Optional[str] = None
    server_version: Optional[str] = None
    sdk_language: str = "Python"
    sdk_version: Optional[str] = None
    ip_address: Optional[str] = None
    identify_actor_given_id: Optional[str] = None
    identify_actor_data: JsonRecord = field(default_factory=dict)
    identify_actor_groups: Optional[Dict[str, str]] = None
