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

from ._context_parameters import (
    add_context_parameter_to_schema,
    get_context_description,
    is_context_enabled,
)
from ._event_types import MCPAnalyticsEventType
from ._exceptions import capture_exception
from ._instrumentation import drain_pending_sync, fire_and_forget
from ._lib_identity import apply_mcp_lib_identity
from ._model_parameters import (
    add_model_parameter_to_schema,
    can_inject_model_parameter,
    get_model_description,
    is_capture_model_enabled,
    normalize_model,
    resolve_model,
)
from ._sink import McpCaptureOptions, McpEventSink
from .tools import build_report_missing_descriptor
from .types import (
    JsonRecord,
    MCPAnalyticsContextOptions,
    MCPAnalyticsModelOptions,
    MCPAnalyticsModelSource,
    PreparedToolCall,
)

__all__ = ["PostHogMCP"]

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
        mcp_exception_autocapture: bool = True,
        capture_model: Union[bool, MCPAnalyticsModelOptions] = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(api_key, **kwargs)
        apply_mcp_lib_identity(self)
        self._mcp_sink = McpEventSink(self)
        self._missing_capability_tool_name = (
            missing_capability_tool_name or _GET_MORE_TOOLS_NAME
        )
        # Whether a failed tool call fans out an `$exception` sibling event. Distinct
        # from the inherited Client.enable_exception_autocapture (global uncaught-error
        # hook); this mirrors instrument()'s enable_exception_autocapture, default on.
        self._mcp_exception_autocapture = mcp_exception_autocapture
        self._capture_model = capture_model
        self._model_parameter_injected: Dict[str, bool] = {}

    # --- lifecycle -----------------------------------------------------------

    def flush(self, timeout_seconds: Optional[float] = 10) -> None:
        """Drain in-flight MCP captures scheduled on the background loop, then flush
        the underlying client. The capture methods are fire-and-forget, so without
        this drain a trailing event could still be in flight at flush time."""
        drain_pending_sync(self, timeout=timeout_seconds)
        return super().flush(timeout_seconds=timeout_seconds)

    def shutdown(self) -> None:
        """Drain in-flight MCP captures, then shut the underlying client down."""
        drain_pending_sync(self)
        return super().shutdown()

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
        error_type: Optional[str] = None,
        category: Optional[str] = None,
        tool_description: Optional[str] = None,
        llm_model: Optional[str] = None,
        llm_model_source: Optional[MCPAnalyticsModelSource] = None,
        protocol_version: Optional[str] = None,
        distinct_id: Optional[str] = None,
        session_id: Optional[str] = None,
        client_user_agent: Optional[str] = None,
        vendor_client: Optional[str] = None,
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
            client_user_agent,
            vendor_client,
        )
        event["resource_name"] = tool_name
        event["tool_description"] = tool_description
        event["tool_category"] = category
        event["protocol_version"] = protocol_version
        event["parameters"] = parameters
        event["response"] = response
        event["duration"] = duration_ms
        event["is_error"] = is_error
        event["error_type"] = error_type
        _apply_intent(event, intent, intent_source)
        _apply_model(event, llm_model, llm_model_source)
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
        protocol_version: Optional[str] = None,
        parameters: Any = None,
        response: Any = None,
        duration_ms: Optional[float] = None,
        distinct_id: Optional[str] = None,
        session_id: Optional[str] = None,
        client_user_agent: Optional[str] = None,
        vendor_client: Optional[str] = None,
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
            client_user_agent,
            vendor_client,
        )
        event["client_name"] = client_name
        event["client_version"] = client_version
        event["protocol_version"] = protocol_version
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
        error_type: Optional[str] = None,
        protocol_version: Optional[str] = None,
        distinct_id: Optional[str] = None,
        session_id: Optional[str] = None,
        client_user_agent: Optional[str] = None,
        vendor_client: Optional[str] = None,
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
            client_user_agent,
            vendor_client,
        )
        event["listed_tool_names"] = tool_names
        event["protocol_version"] = protocol_version
        event["parameters"] = parameters
        event["response"] = response
        event["duration"] = duration_ms
        event["is_error"] = is_error
        event["error_type"] = error_type
        if is_error:
            event["error"] = capture_exception(
                error if error is not None else "tools/list failed"
            )
        self._emit(event)

    def capture_missing_capability(
        self,
        *,
        context: Optional[str] = None,
        llm_model: Optional[str] = None,
        llm_model_source: Optional[MCPAnalyticsModelSource] = None,
        parameters: Any = None,
        protocol_version: Optional[str] = None,
        distinct_id: Optional[str] = None,
        session_id: Optional[str] = None,
        client_user_agent: Optional[str] = None,
        vendor_client: Optional[str] = None,
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
            client_user_agent,
            vendor_client,
        )
        event["resource_name"] = self._missing_capability_tool_name
        event["protocol_version"] = protocol_version
        event["parameters"] = parameters
        _apply_intent(event, context, "context_parameter")
        _apply_model(event, llm_model, llm_model_source)
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
        prepared = []
        context_description = get_context_description(context)
        for tool in tools:
            current = (
                self._inject_context(tool, context_description)
                if is_context_enabled(context)
                else tool
            )
            prepared.append(current)

        if report_missing and not any(
            _tool_name(t) == self._missing_capability_tool_name for t in prepared
        ):
            prepared.append(
                build_report_missing_descriptor(self._missing_capability_tool_name)
            )
        prepared = self._inject_models(prepared)
        return prepared

    def prepare_tool_call(
        self,
        name: str,
        args: Optional[JsonRecord] = None,
        *,
        request_meta: Optional[JsonRecord] = None,
        original_tool: Any = None,
    ) -> PreparedToolCall:
        """Pull the agent's intent off the injected ``context`` argument, strip
        ``context`` from the arguments, and flag the ``get_more_tools`` virtual tool."""
        raw_context = (args or {}).get("context")
        intent = (
            raw_context.strip()
            if isinstance(raw_context, str) and raw_context.strip()
            else None
        )
        analytics_owns_model = False
        if is_capture_model_enabled(self._capture_model):
            if original_tool is not None:
                analytics_owns_model = can_inject_model_parameter(
                    _tool_schema(original_tool)
                )
            else:
                analytics_owns_model = self._model_parameter_injected.get(name, False)
        llm_model, llm_model_source = resolve_model(
            request_meta, args, allow_self_reported=analytics_owns_model
        )
        prepared_args = _strip_context(args)
        if analytics_owns_model:
            prepared_args = _strip_model(prepared_args)
        return PreparedToolCall(
            args=prepared_args,
            intent=intent,
            intent_source="context_parameter" if intent else None,
            llm_model=llm_model,
            llm_model_source=llm_model_source,
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
        client_user_agent: Optional[str] = None,
        vendor_client: Optional[str] = None,
    ) -> Dict[str, Any]:
        event: Dict[str, Any] = {
            "event_type": event_type,
            "session_id": session_id,
            "timestamp": timestamp or datetime.now(timezone.utc),
            "properties": properties,
            "groups": groups,
            # Raw transport headers. A custom dispatcher holds its own request
            # object, so it passes these itself; instrumented servers read them
            # off the request automatically.
            "client_user_agent": client_user_agent,
            "vendor_client": vendor_client,
        }
        if distinct_id:
            event["identify_actor_given_id"] = distinct_id
        if set_properties:
            event["identify_actor_data"] = set_properties
        return event

    def _emit(self, event: Dict[str, Any]) -> None:
        # Fire-and-forget, mirroring posthog-node: never block or raise into the host.
        options = McpCaptureOptions(
            enable_exception_autocapture=self._mcp_exception_autocapture
        )
        # PostHogMCP exposes synchronous lifecycle methods, so always use the shared
        # background loop even when capture is called by an async host. This keeps
        # flush()/shutdown() able to drain without blocking their own event loop's tasks.
        fire_and_forget(self._mcp_sink.capture(event, options), self, background=True)

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

    def _inject_models(self, tools: List[Any]) -> List[Any]:
        self._model_parameter_injected.clear()
        if not is_capture_model_enabled(self._capture_model):
            return tools

        ownership: Dict[str, bool] = {}
        for tool in tools:
            name = _tool_name(tool)
            if name is None:
                continue
            can_inject = can_inject_model_parameter(_tool_schema(tool))
            ownership[name] = ownership.get(name, True) and can_inject
        self._model_parameter_injected.update(ownership)

        return [
            self._inject_model(tool)
            if ownership.get(_tool_name(tool) or "", True)
            else tool
            for tool in tools
        ]

    def _inject_model(self, tool: Any) -> Any:
        name = _tool_name(tool) or "unknown"

        schema = _tool_schema(tool)
        new_schema = add_model_parameter_to_schema(
            schema, name, get_model_description(self._capture_model)
        )
        if isinstance(tool, dict):
            return {**tool, "inputSchema": new_schema}
        try:
            if hasattr(tool, "input_schema"):
                tool.input_schema = new_schema
            else:
                tool.inputSchema = new_schema
        except Exception:  # noqa: BLE001 - read-only descriptors fail closed
            self._model_parameter_injected[name] = False
        return tool


def _apply_intent(
    event: Dict[str, Any], intent: Optional[str], source: Optional[str]
) -> None:
    trimmed = intent.strip() if isinstance(intent, str) else ""
    if not trimmed:
        return
    event["user_intent"] = trimmed
    event["user_intent_source"] = source or "context_parameter"


def _apply_model(
    event: Dict[str, Any],
    model: Optional[str],
    source: Optional[MCPAnalyticsModelSource],
) -> None:
    normalized = normalize_model(model)
    if not normalized:
        return
    event["llm_model"] = normalized
    event["llm_model_source"] = source or "self_reported"


def _strip_context(args: Optional[JsonRecord]) -> Optional[JsonRecord]:
    if not args or "context" not in args:
        return args
    return {k: v for k, v in args.items() if k != "context"}


def _strip_model(args: Optional[JsonRecord]) -> Optional[JsonRecord]:
    if not args or "llm_model" not in args:
        return args
    return {k: v for k, v in args.items() if k != "llm_model"}


def _tool_name(tool: Any) -> Optional[str]:
    if isinstance(tool, dict):
        return tool.get("name")
    return getattr(tool, "name", None)


def _tool_schema(tool: Any) -> Optional[Dict[str, Any]]:
    if isinstance(tool, dict):
        schema = tool.get("inputSchema")
    else:
        schema = getattr(tool, "input_schema", None)
        if schema is None:
            schema = getattr(tool, "inputSchema", None)
    return schema if isinstance(schema, dict) else None
