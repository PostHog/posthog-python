# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""``PostHogMCP`` — a posthog ``Client`` subclass with first-class MCP analytics,
for custom dispatchers (Hono/edge/HTTP) where there is no ``Server``/``FastMCP``
to wrap. The host resolves identity + context per request and calls the capture
methods directly. MCP events flow through the same sanitize -> truncate ->
``$exception`` fan-out pipeline as ``instrument()``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from posthog.client import Client

from .context_parameters import (
    add_context_parameter_to_schema,
    get_context_description,
    is_context_enabled,
)
from .event_types import MCPAnalyticsEventType
from .exceptions import capture_exception
from .instrumentation import fire_and_forget
from .sink import McpCaptureOptions, McpEventSink
from .tools import build_report_missing_descriptor
from .types import (
    JsonRecord,
    MCPAnalyticsContextOptions,
    PreparedToolCall,
)

_GET_MORE_TOOLS_NAME = "get_more_tools"


class PostHogMCP(Client):
    """A drop-in posthog ``Client`` with ``capture_tool_call`` / ``capture_initialize``
    / ``capture_tools_list`` / ``capture_missing_capability`` plus ``prepare_tool_list``
    and ``prepare_tool_call`` helpers. ``capture``, ``flush``, ``shutdown``, feature
    flags, etc. all work unchanged."""

    def __init__(
        self,
        api_key: str,
        missing_capability_tool_name: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(api_key, **kwargs)
        self._mcp_sink = McpEventSink(self)
        self._missing_capability_tool_name = (
            missing_capability_tool_name or _GET_MORE_TOOLS_NAME
        )

    # --- capture methods -----------------------------------------------------

    def capture_tool_call(
        self,
        tool_name: str,
        *,
        intent: Optional[str] = None,
        intent_source: Optional[str] = None,
        parameters: Any = None,
        response: Any = None,
        duration_ms: Optional[float] = None,
        is_error: bool = False,
        error: Any = None,
        category: Optional[str] = None,
        tool_description: Optional[str] = None,
        distinct_id: Optional[str] = None,
        session_id: Optional[str] = None,
        set_properties: Optional[JsonRecord] = None,
        groups: Optional[Dict[str, str]] = None,
        properties: Optional[JsonRecord] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """Capture a tool invocation. Emits ``$mcp_tool_call`` (+ ``$exception`` on error)."""
        event = self._base_event(
            MCPAnalyticsEventType.MCP_TOOLS_CALL,
            distinct_id,
            session_id,
            set_properties,
            groups,
            properties,
            timestamp,
        )
        event["resource_name"] = tool_name
        event["tool_description"] = tool_description
        event["tool_category"] = category
        event["parameters"] = parameters
        event["response"] = response
        event["duration"] = duration_ms
        event["is_error"] = is_error
        _apply_intent(event, intent, intent_source)
        if is_error:
            event["error"] = capture_exception(
                error if error is not None else f"Tool {tool_name} returned an error"
            )
        self._emit(event)

    def capture_initialize(
        self,
        *,
        client_name: Optional[str] = None,
        client_version: Optional[str] = None,
        parameters: Any = None,
        response: Any = None,
        duration_ms: Optional[float] = None,
        distinct_id: Optional[str] = None,
        session_id: Optional[str] = None,
        set_properties: Optional[JsonRecord] = None,
        groups: Optional[Dict[str, str]] = None,
        properties: Optional[JsonRecord] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """Capture the connection handshake. Emits ``$mcp_initialize``."""
        event = self._base_event(
            MCPAnalyticsEventType.MCP_INITIALIZE,
            distinct_id,
            session_id,
            set_properties,
            groups,
            properties,
            timestamp,
        )
        event["client_name"] = client_name
        event["client_version"] = client_version
        event["parameters"] = parameters
        event["response"] = response
        event["duration"] = duration_ms
        self._emit(event)

    def capture_tools_list(
        self,
        *,
        tool_names: Optional[List[str]] = None,
        parameters: Any = None,
        response: Any = None,
        duration_ms: Optional[float] = None,
        is_error: bool = False,
        error: Any = None,
        distinct_id: Optional[str] = None,
        session_id: Optional[str] = None,
        set_properties: Optional[JsonRecord] = None,
        groups: Optional[Dict[str, str]] = None,
        properties: Optional[JsonRecord] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """Capture a ``tools/list`` response. Emits ``$mcp_tools_list`` with the
        advertised tool names (``$mcp_listed_tool_names``)."""
        event = self._base_event(
            MCPAnalyticsEventType.MCP_TOOLS_LIST,
            distinct_id,
            session_id,
            set_properties,
            groups,
            properties,
            timestamp,
        )
        event["listed_tool_names"] = tool_names
        event["parameters"] = parameters
        event["response"] = response
        event["duration"] = duration_ms
        event["is_error"] = is_error
        if is_error:
            event["error"] = capture_exception(
                error if error is not None else "tools/list failed"
            )
        self._emit(event)

    def capture_missing_capability(
        self,
        *,
        context: Optional[str] = None,
        parameters: Any = None,
        distinct_id: Optional[str] = None,
        session_id: Optional[str] = None,
        set_properties: Optional[JsonRecord] = None,
        groups: Optional[Dict[str, str]] = None,
        properties: Optional[JsonRecord] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """Capture a ``get_more_tools`` call as a missing-capability report. Emits
        ``$mcp_missing_capability`` with the agent's description as ``$mcp_intent``."""
        event = self._base_event(
            MCPAnalyticsEventType.MCP_MISSING_CAPABILITY,
            distinct_id,
            session_id,
            set_properties,
            groups,
            properties,
            timestamp,
        )
        event["resource_name"] = self._missing_capability_tool_name
        event["parameters"] = parameters
        _apply_intent(event, context, "context_parameter")
        self._emit(event)

    # --- prepare helpers -----------------------------------------------------

    def prepare_tool_list(
        self,
        tools: List[Any],
        context: Union[bool, MCPAnalyticsContextOptions] = True,
        report_missing: bool = False,
    ) -> List[Any]:
        """Inject the ``context`` argument into every tool so agents state their
        intent (captured as ``$mcp_intent``), and optionally append the
        ``get_more_tools`` virtual tool (``report_missing=True``). Returns a new
        list; dict tools are copied, tool objects are mutated in place."""
        if is_context_enabled(context):
            description = get_context_description(context)
            prepared = [self._inject_context(tool, description) for tool in tools]
        else:
            prepared = list(tools)

        if report_missing and not any(
            _tool_name(t) == self._missing_capability_tool_name for t in prepared
        ):
            prepared.append(
                build_report_missing_descriptor(self._missing_capability_tool_name)
            )
        return prepared

    def prepare_tool_call(
        self, name: str, args: Optional[JsonRecord] = None
    ) -> PreparedToolCall:
        """Pull the agent's intent off the injected ``context`` argument, strip
        ``context`` from the arguments, and flag the ``get_more_tools`` virtual tool."""
        raw_context = (args or {}).get("context")
        intent = (
            raw_context.strip()
            if isinstance(raw_context, str) and raw_context.strip()
            else None
        )
        return PreparedToolCall(
            args=_strip_context(args),
            intent=intent,
            intent_source="context_parameter" if intent else None,
            is_missing_capability=name == self._missing_capability_tool_name,
        )

    # --- internals -----------------------------------------------------------

    def _base_event(
        self,
        event_type: str,
        distinct_id: Optional[str],
        session_id: Optional[str],
        set_properties: Optional[JsonRecord],
        groups: Optional[Dict[str, str]],
        properties: Optional[JsonRecord],
        timestamp: Optional[datetime],
    ) -> Dict[str, Any]:
        event: Dict[str, Any] = {
            "event_type": event_type,
            "session_id": session_id,
            "timestamp": timestamp or datetime.now(timezone.utc),
            "properties": properties,
            "groups": groups,
        }
        if distinct_id:
            event["identify_actor_given_id"] = distinct_id
        if set_properties:
            event["identify_actor_data"] = set_properties
        return event

    def _emit(self, event: Dict[str, Any]) -> None:
        # Fire-and-forget, mirroring posthog-node: never block or raise into the host.
        options = McpCaptureOptions(enable_exception_autocapture=True)
        fire_and_forget(self._mcp_sink.capture(event, options))

    def _inject_context(self, tool: Any, description: Optional[str]) -> Any:
        if isinstance(tool, dict):
            name = tool.get("name", "unknown")
            if name == self._missing_capability_tool_name:
                return tool
            new_schema = add_context_parameter_to_schema(
                tool.get("inputSchema"), name, description
            )
            return {**tool, "inputSchema": new_schema}

        name = getattr(tool, "name", "unknown")
        if name == self._missing_capability_tool_name:
            return tool
        new_schema = add_context_parameter_to_schema(
            getattr(tool, "inputSchema", None), name, description
        )
        try:
            tool.inputSchema = new_schema
        except Exception:  # noqa: BLE001
            pass
        return tool


def _apply_intent(
    event: Dict[str, Any], intent: Optional[str], source: Optional[str]
) -> None:
    trimmed = intent.strip() if isinstance(intent, str) else ""
    if not trimmed:
        return
    event["user_intent"] = trimmed
    event["user_intent_source"] = source or "context_parameter"


def _strip_context(args: Optional[JsonRecord]) -> Optional[JsonRecord]:
    if not args or "context" not in args:
        return args
    return {k: v for k, v in args.items() if k != "context"}


def _tool_name(tool: Any) -> Optional[str]:
    if isinstance(tool, dict):
        return tool.get("name")
    return getattr(tool, "name", None)
