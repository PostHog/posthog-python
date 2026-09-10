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
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    TypedDict,
    Union,
)

from .logger import LoggerFn

__all__ = [
    "MCPAnalyticsOptions",
    "MCPAnalyticsContextOptions",
    "MCPAnalyticsModelOptions",
    "MCPAnalyticsModelSource",
    "UserIdentity",
    "CaptureEventData",
    "CollectFeedbackConfig",
    "CollectFeedbackOptions",
    "FeedbackReport",
    "FeedbackSentiment",
    "FeedbackType",
    "PreparedToolCall",
]

JsonRecord = Dict[str, Any]

# PostHog error-tracking properties (the ``$exception_list`` / ``$exception_level`` shape).
ErrorProperties = Dict[str, Any]

MCPAnalyticsIntentSource = str  # "context_parameter" | "inferred"
MCPAnalyticsModelSource = Literal["client_metadata", "self_reported"]

# Internal MCP event as it flows through the SDK before capture. Modeled as a
# plain dict (constructed and read with ``.get()`` throughout) to mirror the TS
# plain-object pipeline. Snake_case keys map to the ``$mcp_*`` wire keys in
# ``posthog_events``. Known keys: client_name, client_version, conversation_id,
# duration, error, error_type, event_name, event_type, groups, id, identify_actor_data,
# identify_actor_given_id, is_error, listed_tool_names, parameters, properties,
# resource_name, response, server_name, server_version, session_id, timestamp,
# tool_category, tool_description, user_intent, user_intent_source, llm_model,
# llm_model_source.
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


@dataclass
class MCPAnalyticsModelOptions:
    """Configure the model field injected into tool input schemas."""

    description: Optional[str] = None


FeedbackType = Literal["missing_capability", "issue", "praise", "other"]
FeedbackSentiment = Literal["positive", "neutral", "negative", "mixed"]

# Route each report to a real backend. Return a string (sync or async) to replace
# the default acknowledgement text; a raise is logged and falls back to it.
OnFeedbackFn = Callable[["FeedbackReport"], Any]  # -> Optional[str] | awaitable


@dataclass
class CollectFeedbackOptions:
    """Object form of the ``collect_feedback`` option (``True`` uses the defaults)."""

    # Rename the ``send_feedback`` virtual tool. Set once so the tool is
    # advertised and detected under the same name.
    tool_name: Optional[str] = None
    # Replace the default tool description.
    description: Optional[str] = None
    # Host-specific fields merged into the tool's advertised input schema (plain
    # JSON Schema fragments, keyed by property name). Each declared key is
    # captured as a ``$mcp_feedback_<key>`` event property through the standard
    # sanitize/redact/truncate pipeline; arguments the agent invents beyond the
    # schema are never captured. A key that collides with a core field or an
    # SDK-injected argument raises at configuration time.
    extra_properties: Optional[Dict[str, Dict[str, Any]]] = None
    # Keys of ``extra_properties`` to advertise as required.
    extra_required: Optional[List[str]] = None
    # ``instrument()`` path only — a custom dispatcher routes reports itself via
    # :attr:`PreparedToolCall.feedback_report`. The ``$mcp_feedback`` event is
    # captured whether or not the handler raises.
    on_feedback: Optional[OnFeedbackFn] = None


# The ``collect_feedback`` option: ``True``/``False`` or the object form.
CollectFeedbackConfig = Union[bool, CollectFeedbackOptions]


@dataclass
class FeedbackReport:
    """One parsed ``send_feedback`` call, as handed to ``on_feedback`` and the
    custom dispatcher."""

    # Invalid or missing values fall back to ``other``.
    feedback_type: str = "other"
    # One-sentence summary; empty string when the agent omitted it.
    summary: str = ""
    sentiment: Optional[str] = None
    friction_points: Optional[str] = None
    suggested_improvement: Optional[str] = None
    details: Optional[str] = None
    # The existing tool the feedback is about (the ``tool_name`` argument).
    tool_name: Optional[str] = None
    task_completed: Optional[bool] = None
    # Values of the declared ``extra_properties`` fields.
    extras: JsonRecord = field(default_factory=dict)
    # The full raw arguments, for the handler only — never captured.
    raw: JsonRecord = field(default_factory=dict)


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
    # Capture the model from recognized client metadata, falling back to an
    # SDK-injected llm_model argument. Off by default.
    capture_model: Union[bool, MCPAnalyticsModelOptions] = False
    # Inject the `send_feedback` virtual tool so agents can send feedback about
    # this server to its developers — a missing capability (the priority
    # category), a tool that failed or confused them, or praise. Calls to it emit
    # `$mcp_feedback` (never a `$mcp_tool_call`). Off by default. `True` uses the
    # defaults; the object form renames the tool, replaces its description,
    # declares host-specific extra_properties, or wires an on_feedback handler.
    # Covers what `report_missing` covers (as feedback_type "missing_capability"),
    # so new integrations should enable only one of the two. New field appended
    # last: positional construction of the earlier fields must keep working.
    collect_feedback: Union[bool, CollectFeedbackOptions] = False


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
    llm_model: Optional[str] = None
    llm_model_source: Optional[MCPAnalyticsModelSource] = None
    # True when the call targeted the ``send_feedback`` virtual tool AND the
    # constructor's ``collect_feedback`` option is set. Always False without that
    # opt-in, so a real tool that happens to use the name is never shadowed.
    is_feedback: bool = False
    # The parsed report, set only when ``is_feedback`` is True. Pass it to
    # ``PostHogMCP.capture_feedback`` and to your own feedback backend, then
    # reply with ``send_feedback_result()`` or a custom text.
    feedback_report: Optional[FeedbackReport] = None


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
