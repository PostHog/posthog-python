# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Translate a processed internal ``Event`` into 1-2 ``PostHogCaptureEvent``
payloads (the main ``$mcp_*`` event plus an optional ``$exception`` sibling)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from .constants import (
    POSTHOG_MCP_ANALYTICS_SOURCE,
    PostHogMCPAnalyticsEvent,
    PostHogMCPAnalyticsProperty,
)
from ._event_types import MCPAnalyticsEventType
from .types import Event, PostHogCaptureEvent

_BUILT_IN_EVENT_NAME_BY_TYPE = {
    MCPAnalyticsEventType.CUSTOM: PostHogMCPAnalyticsEvent.CUSTOM,
    MCPAnalyticsEventType.IDENTIFY: PostHogMCPAnalyticsEvent.IDENTIFY,
    MCPAnalyticsEventType.MCP_MISSING_CAPABILITY: PostHogMCPAnalyticsEvent.MISSING_CAPABILITY,
    MCPAnalyticsEventType.MCP_INITIALIZE: PostHogMCPAnalyticsEvent.INITIALIZE,
    MCPAnalyticsEventType.MCP_PROMPTS_GET: PostHogMCPAnalyticsEvent.PROMPT_GET,
    MCPAnalyticsEventType.MCP_PROMPTS_LIST: PostHogMCPAnalyticsEvent.PROMPTS_LIST,
    MCPAnalyticsEventType.MCP_RESOURCES_LIST: PostHogMCPAnalyticsEvent.RESOURCES_LIST,
    MCPAnalyticsEventType.MCP_RESOURCES_READ: PostHogMCPAnalyticsEvent.RESOURCE_READ,
    MCPAnalyticsEventType.MCP_TOOLS_CALL: PostHogMCPAnalyticsEvent.TOOL_CALL,
    MCPAnalyticsEventType.MCP_TOOLS_LIST: PostHogMCPAnalyticsEvent.TOOLS_LIST,
}

_P = PostHogMCPAnalyticsProperty


def _get_distinct_id(event: Event) -> str:
    return (
        event.get("identify_actor_given_id") or event.get("session_id") or "anonymous"
    )


def _get_timestamp(event: Event) -> datetime:
    return event.get("timestamp") or datetime.now(timezone.utc)


def build_posthog_capture_events(
    event: Event, enable_exception_autocapture: bool = True
) -> List[PostHogCaptureEvent]:
    batch = [_build_capture_event(event)]
    if (
        event.get("is_error")
        and event.get("error")
        and enable_exception_autocapture is not False
    ):
        batch.append(_build_exception_event(event))
    return batch


def _build_capture_event(event: Event) -> PostHogCaptureEvent:
    properties: Dict[str, Any] = {_P.SOURCE: POSTHOG_MCP_ANALYTICS_SOURCE}
    _add_session_id(event, properties)
    _add_conversation_id(event, properties)
    _add_person_processing(event, properties)
    _add_groups(event, properties)
    _add_common_properties(event, properties)
    _add_custom_properties(event, properties)

    event_name = (
        event.get("event_name") or _BUILT_IN_EVENT_NAME_BY_TYPE[event["event_type"]]
    )
    return {
        "event": event_name,
        "distinct_id": _get_distinct_id(event),
        "properties": properties,
        "timestamp": _get_timestamp(event),
    }


def _add_session_id(event: Event, properties: Dict[str, Any]) -> None:
    session_id = event.get("session_id")
    if isinstance(session_id, str) and len(session_id) > 0:
        properties[_P.SESSION_ID] = session_id


def _add_conversation_id(event: Event, properties: Dict[str, Any]) -> None:
    conversation_id = event.get("conversation_id")
    if conversation_id is not None and conversation_id != "":
        properties[_P.CONVERSATION_ID] = conversation_id


def _add_groups(event: Event, properties: Dict[str, Any]) -> None:
    groups = event.get("groups")
    if groups:
        properties["$groups"] = groups


def _add_person_processing(event: Event, properties: Dict[str, Any]) -> None:
    # Without a resolved identity the distinct id is just the session id, so
    # processing a person profile would mint one anonymous person per session.
    if not event.get("identify_actor_given_id"):
        properties["$process_person_profile"] = False


def _is_tool_call(event: Event) -> bool:
    return event.get("event_type") == MCPAnalyticsEventType.MCP_TOOLS_CALL


def _add_common_properties(event: Event, properties: Dict[str, Any]) -> None:
    if event.get("resource_name"):
        properties[_P.RESOURCE_NAME] = event["resource_name"]
        if _is_tool_call(event):
            properties[_P.TOOL_NAME] = event["resource_name"]
    if event.get("tool_description") and _is_tool_call(event):
        properties[_P.TOOL_DESCRIPTION] = event["tool_description"]
    if event.get("tool_category") and _is_tool_call(event):
        properties[_P.TOOL_CATEGORY] = event["tool_category"]
    if (
        event.get("listed_tool_names")
        and len(event["listed_tool_names"]) > 0
        and event.get("event_type") == MCPAnalyticsEventType.MCP_TOOLS_LIST
    ):
        properties[_P.LISTED_TOOL_NAMES] = event["listed_tool_names"]
    if event.get("duration") is not None:
        properties[_P.DURATION_MS] = event["duration"]
    if event.get("server_name"):
        properties[_P.SERVER_NAME] = event["server_name"]
    if event.get("server_version"):
        properties[_P.SERVER_VERSION] = event["server_version"]
    if event.get("client_name"):
        properties[_P.CLIENT_NAME] = event["client_name"]
    if event.get("client_version"):
        properties[_P.CLIENT_VERSION] = event["client_version"]
    # HTTP transports only, and only for the request that carried the header —
    # stdio and in-memory servers simply never set these.
    if event.get("client_user_agent"):
        properties[_P.CLIENT_USER_AGENT] = event["client_user_agent"]
    if event.get("vendor_client"):
        properties[_P.VENDOR_CLIENT] = event["vendor_client"]
    if event.get("protocol_version"):
        properties[_P.PROTOCOL_VERSION] = event["protocol_version"]
    if event.get("user_intent"):
        properties[_P.INTENT] = event["user_intent"]
    if event.get("user_intent_source"):
        properties[_P.INTENT_SOURCE] = event["user_intent_source"]
    if event.get("is_error") is not None:
        properties[_P.IS_ERROR] = event["is_error"]
    if event.get("is_error"):
        _add_error_details(event, properties)
    if event.get("parameters") is not None:
        properties[_P.PARAMETERS] = event["parameters"]
    if event.get("response") is not None:
        properties[_P.RESPONSE] = event["response"]
    identify_actor_data = event.get("identify_actor_data")
    if identify_actor_data and len(identify_actor_data) > 0:
        # Person properties from identify().properties go straight to $set.
        properties["$set"] = {**identify_actor_data}


_TOOL_DISPATCH_WRAPPERS = ("ToolError", "UnexpectedToolError")

# Where the SDKs define their dispatch wrappers: mcp.server.fastmcp.exceptions
# (mcp 1.x), mcp.server.mcpserver.exceptions (mcp 2.x), fastmcp.exceptions
# (standalone fastmcp). An application's own exception carries its own module,
# so a matching name alone must not unwrap it.
_SDK_MODULE_PREFIXES = ("mcp.", "fastmcp.")


def _is_dispatch_wrapper(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    return (
        entry.get("type") in _TOOL_DISPATCH_WRAPPERS
        and str(entry.get("module") or "").startswith(_SDK_MODULE_PREFIXES)
        and str(entry.get("value", "")).startswith("Error executing tool")
    )


def _primary_exception(error: Any) -> Dict[str, Any]:
    """Pick the ``$exception_list`` entry that carries the failure reason.

    The MCP SDK's tool dispatch re-raises whatever a tool raised as a
    ``ToolError`` whose message starts ``Error executing tool <name>``, and
    mcp >= 2.1 masks the original text out of that message entirely, keeping
    it only on ``__cause__`` — the next entry of the chain here. The wrapper
    says nothing the event's tool name does not already say, so the scalars
    step past every consecutive wrapper (a tool invoking a failing tool is
    wrapped once per dispatch) to the first real exception.
    """
    if not isinstance(error, dict):
        return {}
    exception_list = error.get("$exception_list")
    if not isinstance(exception_list, list) or not exception_list:
        return {}
    index = 0
    while (
        index + 1 < len(exception_list)
        and isinstance(exception_list[index + 1], dict)
        and _is_dispatch_wrapper(exception_list[index])
    ):
        index += 1
    entry = exception_list[index]
    return entry if isinstance(entry, dict) else {}


def _add_error_details(event: Event, properties: Dict[str, Any]) -> None:
    """Surface the failure reason on the primary event itself.

    Without these the dashboard has to join to the ``$exception`` sibling to
    know *why* a call failed — and that sibling can be switched off with
    ``enable_exception_autocapture``, or never emitted when no error value was
    passed. Both values are read off the ``$exception_list`` the sibling would
    carry — the sibling keeps the full chain while the scalars carry the entry
    ``_primary_exception`` picks; the message is already bounded to
    ``_MAX_ERROR_MESSAGE_LENGTH`` because truncation runs before this mapping.
    """
    first = _primary_exception(event.get("error"))

    # An explicit coarse category (e.g. "validation", "timeout") beats the
    # thrown type; a custom dispatcher can pass one that means something to the
    # product, where the class name rarely does.
    error_type = event.get("error_type") or first.get("type")
    if error_type:
        properties[_P.ERROR_TYPE] = error_type
    message = first.get("value")
    if message:
        properties[_P.ERROR_MESSAGE] = message


def _add_custom_properties(event: Event, properties: Dict[str, Any]) -> None:
    custom = event.get("properties")
    if custom:
        for key, value in custom.items():
            properties[key] = value


def _build_exception_event(event: Event) -> PostHogCaptureEvent:
    properties: Dict[str, Any] = {}
    _add_session_id(event, properties)
    _add_conversation_id(event, properties)
    _add_person_processing(event, properties)
    _add_groups(event, properties)

    error = event.get("error")
    if error:
        # Spread the core $exception_list / $exception_level so MCP tool failures
        # use the same error-tracking contract as every other SDK.
        properties.update(error)

    if event.get("resource_name"):
        properties[_P.RESOURCE_NAME] = event["resource_name"]
        if _is_tool_call(event):
            properties[_P.TOOL_NAME] = event["resource_name"]
    if event.get("tool_description") and _is_tool_call(event):
        properties[_P.TOOL_DESCRIPTION] = event["tool_description"]
    if event.get("tool_category") and _is_tool_call(event):
        properties[_P.TOOL_CATEGORY] = event["tool_category"]
    if event.get("server_name"):
        properties[_P.SERVER_NAME] = event["server_name"]
    if event.get("server_version"):
        properties[_P.SERVER_VERSION] = event["server_version"]
    if event.get("client_name"):
        properties[_P.CLIENT_NAME] = event["client_name"]
    if event.get("client_version"):
        properties[_P.CLIENT_VERSION] = event["client_version"]
    if event.get("protocol_version"):
        properties[_P.PROTOCOL_VERSION] = event["protocol_version"]

    _add_custom_properties(event, properties)

    return {
        "event": PostHogMCPAnalyticsEvent.EXCEPTION,
        "distinct_id": _get_distinct_id(event),
        "properties": properties,
        "timestamp": _get_timestamp(event),
    }
