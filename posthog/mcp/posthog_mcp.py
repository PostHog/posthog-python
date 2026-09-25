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

import copy
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from posthog.client import Client

from ._context_parameters import (
    add_context_parameter_to_schema,
    get_context_description,
    is_context_enabled,
)
from ._conversation_id import (
    add_conversation_id_to_schema,
    build_prompt_back,
    can_inject_conversation_id,
    inject_prompt_back,
    resolve_conversation_id,
)
from ._event_types import MCPAnalyticsEventType
from ._exceptions import capture_exception
from ._instrumentation import (
    VIRTUAL_TOOL_FEEDBACK,
    VIRTUAL_TOOL_MISSING_CAPABILITY,
    VirtualToolCollisionVariant,
    drain_pending_sync,
    fire_and_forget,
    virtual_tool_collision_message,
)
from ._lib_identity import apply_mcp_lib_identity
from .logger import log, warn
from ._model_parameters import (
    add_model_parameter_to_schema,
    can_inject_model_parameter,
    get_model_description,
    is_capture_model_enabled,
    normalize_model,
    resolve_model,
)
from ._output_instructions import (
    add_instructions_to_output_schema,
    can_declare_output_instructions,
    declare_output_instructions,
    mirror_instructions_into_structured_content,
    tool_output_schema,
)
from ._sink import McpCaptureOptions, McpEventSink
from .feedback import (
    build_feedback_event_properties,
    build_feedback_intent,
    get_feedback_tool_descriptor,
    parse_feedback_report,
    resolve_collect_feedback_options,
    resolve_send_feedback_tool_name,
)
from .session import derive_session_id_from_conversation
from .tools import build_report_missing_descriptor
from .types import (
    CollectFeedbackOptions,
    FeedbackReport,
    JsonRecord,
    MCPAnalyticsContextOptions,
    MCPAnalyticsModelOptions,
    MCPAnalyticsModelSource,
    PreparedConversationState,
    PreparedToolCall,
    PreparedToolResult,
    TResult,
)

__all__ = ["PostHogMCP"]

_GET_MORE_TOOLS_NAME = "get_more_tools"


@dataclass(frozen=True)
class _ConversationOwnership:
    """Whether the SDK owns a tool's ``conversation_id`` input and can declare
    ``_mcp_instructions`` on its output schema."""

    conversation_id: bool
    output_instructions: bool


_NOT_OWNED = _ConversationOwnership(conversation_id=False, output_instructions=False)


class PostHogMCP(Client):
    """A drop-in posthog ``Client`` with ``capture_tool_call`` / ``capture_initialize``
    / ``capture_tools_list`` / ``capture_missing_capability`` / ``capture_feedback``
    plus ``prepare_tool_list``, ``prepare_tool_call`` and ``prepare_tool_result``
    helpers. ``capture``,
    ``flush``, ``shutdown``, feature flags, etc. all work unchanged."""

    def __init__(
        self,
        api_key: str,
        missing_capability_tool_name: Optional[str] = None,
        mcp_exception_autocapture: bool = True,
        capture_model: Union[bool, MCPAnalyticsModelOptions] = True,
        collect_feedback: Union[bool, CollectFeedbackOptions] = False,
        enable_conversation_id: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(api_key, **kwargs)
        apply_mcp_lib_identity(self)
        self._mcp_sink = McpEventSink(self)
        self._missing_capability_tool_name = (
            missing_capability_tool_name or _GET_MORE_TOOLS_NAME
        )
        # `None` is the enable switch's off state: without it, prepare_tool_call
        # must never claim a call named like the virtual tool — the host may have
        # a real tool by that name, and flagging it would shadow the real handler.
        # `on_feedback` is ignored on this path: the host dispatcher routes
        # reports itself via PreparedToolCall.feedback_report.
        self._collect_feedback = resolve_collect_feedback_options(collect_feedback)
        self._feedback_tool_name = resolve_send_feedback_tool_name(
            self._collect_feedback
        )
        # Fail fast on a config error (reserved extra key, undeclared
        # extra_required) instead of first surfacing it when a tools/list is served.
        if self._collect_feedback is not None:
            get_feedback_tool_descriptor(self._collect_feedback)
            if self._collect_feedback.on_feedback is not None:
                log(
                    "Warning: collect_feedback.on_feedback is ignored on the PostHogMCP "
                    "path - route reports from your dispatcher via "
                    "prepare_tool_call().feedback_report instead."
                )
        # Whether a failed tool call fans out an `$exception` sibling event. Distinct
        # from the inherited Client.enable_exception_autocapture (global uncaught-error
        # hook); this mirrors instrument()'s enable_exception_autocapture, default on.
        self._mcp_exception_autocapture = mcp_exception_autocapture
        self._capture_model = capture_model
        self._model_parameter_injected: Dict[str, bool] = {}
        # Correlate calls through an agent-carried `conversation_id` and a derived
        # session id. Off leaves schemas, arguments, results, and capture unchanged.
        self._enable_conversation_id = enable_conversation_id
        self._conversation_ownership: Dict[str, _ConversationOwnership] = {}
        # (kind, name) collision warnings already emitted from prepare_tool_list,
        # so a host that prepares a listing per request logs each once.
        self._warned_virtual_tool_collisions: Set[Tuple[str, str]] = set()

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
        conversation_id: Optional[str] = None,
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
            conversation_id,
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
        conversation_id: Optional[str] = None,
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
            conversation_id,
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
        conversation_id: Optional[str] = None,
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
            conversation_id,
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
        conversation_id: Optional[str] = None,
        client_user_agent: Optional[str] = None,
        vendor_client: Optional[str] = None,
        set_properties: Optional[JsonRecord] = None,
        groups: Optional[Dict[str, str]] = None,
        properties: Optional[JsonRecord] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """Capture a ``get_more_tools`` call as a missing-capability report. Emits
        ``$mcp_missing_capability`` with the agent's description as ``$mcp_intent``.
        Reply to the agent with ``get_more_tools_result()`` after passing it
        through :meth:`prepare_tool_result`."""
        event = self._base_event(
            MCPAnalyticsEventType.MCP_MISSING_CAPABILITY,
            distinct_id,
            session_id,
            conversation_id,
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

    def capture_feedback(
        self,
        *,
        report: FeedbackReport,
        llm_model: Optional[str] = None,
        llm_model_source: Optional[MCPAnalyticsModelSource] = None,
        protocol_version: Optional[str] = None,
        distinct_id: Optional[str] = None,
        session_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        client_user_agent: Optional[str] = None,
        vendor_client: Optional[str] = None,
        set_properties: Optional[JsonRecord] = None,
        groups: Optional[Dict[str, str]] = None,
        properties: Optional[JsonRecord] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """Capture a ``send_feedback`` call as an agent-feedback report. Emits
        ``$mcp_feedback`` with the report's ``$mcp_feedback_*`` properties and its
        summary/details as ``$mcp_intent``. Reply to the agent with
        ``send_feedback_result()`` (or a custom text) after routing the report to
        your own feedback backend and passing the reply through
        :meth:`prepare_tool_result`."""
        event = self._base_event(
            MCPAnalyticsEventType.MCP_FEEDBACK,
            distinct_id,
            session_id,
            conversation_id,
            set_properties,
            groups,
            properties,
            timestamp,
            client_user_agent,
            vendor_client,
        )
        event["resource_name"] = self._feedback_tool_name
        event["protocol_version"] = protocol_version
        # Deliberately no `parameters`: the arguments are agent-narrated free
        # text, and the PII-redacted `$mcp_feedback_*` properties are the captured
        # surface. Raw arguments would bypass that redaction. Feedback properties
        # win over the caller's, matching the instrument() path's merge order.
        event["properties"] = {
            **(properties or {}),
            **build_feedback_event_properties(report),
        }
        _apply_intent(event, build_feedback_intent(report), "context_parameter")
        _apply_model(event, llm_model, llm_model_source)
        self._emit(event)

    # --- prepare helpers -----------------------------------------------------

    def prepare_tool_list(
        self,
        tools: List[Any],
        context: Union[bool, MCPAnalyticsContextOptions] = True,
        report_missing: bool = False,
        collect_feedback: bool = False,
    ) -> List[Any]:
        """Inject the ``context`` argument into every tool so agents state their
        intent (captured as ``$mcp_intent``), and optionally append the
        ``get_more_tools`` virtual tool (``report_missing=True``) and the
        ``send_feedback`` virtual tool (``collect_feedback=True``, which also
        requires the constructor's ``collect_feedback`` option — the enable switch
        that gates detection in :meth:`prepare_tool_call`). By default it also
        injects the optional ``conversation_id`` argument and declares
        ``_mcp_instructions`` on compatible output schemas. Returns a new list;
        dict tools are copied, context injection mutates tool objects in place,
        and model and conversation injection copy them to preserve field
        ownership.

        Pair it with :meth:`prepare_tool_call` on the inbound side and
        :meth:`prepare_tool_result` on the outbound side.

        **On a paginated listing, pass the two switches for the first page only** —
        a client concatenates every page into one list::

            first_page = request.params.get("cursor") is None
            tools = posthog.prepare_tool_list(
                page_tools, report_missing=first_page, collect_feedback=first_page
            )

        A real tool already using a virtual tool's name wins: it is left alone,
        nothing is appended, and a warning names the option that renames
        PostHog's tool."""
        prepared = []
        context_description = get_context_description(context)
        for tool in tools:
            current = (
                self._inject_context(tool, context_description)
                if is_context_enabled(context)
                else tool
            )
            prepared.append(current)

        # A tool already using the name blocks injection — unless it is our own
        # descriptor, which a host re-preparing an already-prepared list hands
        # straight back. Warning about that would be warning about ourselves.
        if report_missing:
            name = self._missing_capability_tool_name
            existing = _find_tool(prepared, name)
            if existing is not None and not self._is_sdk_virtual_tool(existing):
                self._warn_virtual_tool_collision(
                    VIRTUAL_TOOL_MISSING_CAPABILITY,
                    name,
                    'PostHogMCP(missing_capability_tool_name="...")',
                )
            elif existing is None:
                prepared.append(build_report_missing_descriptor(name))
        if collect_feedback and self._collect_feedback is not None:
            name = self._feedback_tool_name
            existing = _find_tool(prepared, name)
            # Both virtual tools under one name: missing-capability wins in
            # `prepare_tool_call`, so advertising this one too would dead-letter
            # the feedback path. Same precedence as `instrument()`.
            duplicate = name == self._missing_capability_tool_name
            if duplicate or (
                existing is not None and not self._is_sdk_virtual_tool(existing)
            ):
                self._warn_virtual_tool_collision(
                    VIRTUAL_TOOL_FEEDBACK,
                    name,
                    'PostHogMCP(collect_feedback=CollectFeedbackOptions(tool_name="..."))',
                    variant="duplicate" if duplicate else "blocked",
                )
            elif existing is None:
                prepared.append(get_feedback_tool_descriptor(self._collect_feedback))
        # Read ownership before any analytics field lands on the schemas.
        conversation_ownership = _collect_conversation_ownership(prepared)
        prepared = self._inject_models(prepared)
        prepared = self._inject_conversation(prepared, conversation_ownership)
        return prepared

    def _warn_virtual_tool_collision(
        self,
        kind: str,
        name: str,
        rename_option: str,
        variant: VirtualToolCollisionVariant = "blocked",
    ) -> None:
        """Warn once per ``(kind, name)`` for this client's lifetime, so a host
        that prepares a listing on every request doesn't flood the log."""
        key = (kind, name)
        if key in self._warned_virtual_tool_collisions:
            return
        self._warned_virtual_tool_collisions.add(key)
        warn(
            virtual_tool_collision_message(
                kind, name, variant, rename_option=rename_option
            )
        )

    def prepare_tool_call(
        self,
        name: str,
        args: Optional[JsonRecord] = None,
        *,
        request_meta: Optional[JsonRecord] = None,
        original_tool: Any = None,
        session_id: Optional[str] = None,
    ) -> PreparedToolCall:
        """Pull the agent's intent off the injected ``context`` argument, strip
        ``context`` from the arguments, and flag the ``get_more_tools`` and
        ``send_feedback`` virtual tools (the latter only with the constructor's
        ``collect_feedback`` opt-in, so a real tool by that name is never
        shadowed). When model capture is enabled, resolve its value and source and
        strip the SDK-owned ``llm_model`` argument before dispatch. When
        conversation correlation is enabled, validate an echoed
        ``conversation_id`` or mint a new one, strip the SDK-owned argument, and
        derive the session id from it.

        Dispatch the returned ``args`` to your tool. Then pass the tool result and
        this prepared call to :meth:`prepare_tool_result`. Return its ``result``
        and capture with its ``session_id`` and ``conversation_id``::

            call = posthog.prepare_tool_call(name, raw_args, original_tool=tool)
            prepared = posthog.prepare_tool_result(run_tool(name, call.args), call)
            posthog.capture_tool_call(
                name,
                intent=call.intent,
                session_id=prepared.session_id,
                conversation_id=prepared.conversation_id,
            )
            return prepared.result

        ``original_tool`` is the application's own tool for ``name``, from the
        host's un-prepared list (the virtual tools never exist there). Passing it
        keeps ownership accurate when ``tools/list`` and ``tools/call`` reach
        different replicas. It also disambiguates a name collision: a real tool by
        the feedback tool's name is dispatched normally instead of being flagged
        as feedback.

        ``session_id`` is a session carried by the request or transport. A valid
        echoed ``conversation_id`` takes precedence. Otherwise the carried session
        stays, and no new handle is minted."""
        raw_context = (args or {}).get("context")
        intent = (
            raw_context.strip()
            if isinstance(raw_context, str) and raw_context.strip()
            else None
        )
        analytics_owns_model = False
        llm_model: Optional[str] = None
        llm_model_source: Optional[MCPAnalyticsModelSource] = None
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

        ownership = (
            _conversation_ownership_of(original_tool)
            if original_tool is not None
            else self._conversation_ownership.get(name)
        )
        # Reads fail open on unknown ownership; strips need a positive answer.
        can_read_conversation = self._enable_conversation_id and (
            ownership is None or ownership.conversation_id
        )
        conversation_id, minted = resolve_conversation_id(can_read_conversation, args)
        # A carried session stays stable until the agent echoes its own handle.
        if minted and session_id:
            conversation_id, minted = None, False
        if conversation_id:
            session_id = derive_session_id_from_conversation(conversation_id)
        if (
            self._enable_conversation_id
            and ownership is not None
            and ownership.conversation_id
        ):
            prepared_args = _strip_conversation_id(prepared_args)
        # A supplied `original_tool` is a real application tool by this name (it
        # comes from the host's own list, which never holds a virtual tool), so
        # the real tool wins — the stateless twin of the ownership check
        # instrument() runs. Without it the name match stands, and the
        # documented remedy for a collision is renaming PostHog's tool.
        # The name check against missing-capability keeps one precedence when
        # both tools share a name: without it both flags are true, and a
        # dispatcher testing `is_feedback` first misroutes every call.
        is_feedback = (
            self._collect_feedback is not None
            and name == self._feedback_tool_name
            and name != self._missing_capability_tool_name
            and original_tool is None
        )
        # Same guard for the missing-capability tool. Unlike feedback it has no
        # constructor enable switch on this path (the name is always populated),
        # so `original_tool` is the only ownership signal available here.
        is_missing_capability = (
            name == self._missing_capability_tool_name and original_tool is None
        )
        return PreparedToolCall(
            args=prepared_args,
            intent=intent,
            intent_source="context_parameter" if intent else None,
            llm_model=llm_model,
            llm_model_source=llm_model_source,
            is_missing_capability=is_missing_capability,
            is_feedback=is_feedback,
            feedback_report=(
                parse_feedback_report(args, self._collect_feedback)
                if is_feedback
                else None
            ),
            session_id=session_id,
            conversation_id=conversation_id,
            _conversation_state=PreparedConversationState(
                minted=minted,
                output_instructions=self._enable_conversation_id
                and ownership is not None
                and ownership.output_instructions,
            ),
        )

    def prepare_tool_result(
        self, result: TResult, prepared_call: PreparedToolCall
    ) -> PreparedToolResult[TResult]:
        """Add the conversation handle to a tool result without changing the
        original value. A newly minted handle is appended to the text
        ``content`` once. When the advertised output schema declares it, the
        handle is also mirrored into ``structuredContent`` on every result.

        Return the prepared ``result`` to the client, and capture with its
        ``session_id`` and ``conversation_id``. If a new handle could not reach
        the client, ``conversation_id`` is omitted and the derived
        ``session_id`` is kept."""
        conversation_id = prepared_call.conversation_id
        session_id = prepared_call.session_id
        state = prepared_call._conversation_state
        if not conversation_id:
            return PreparedToolResult(result, session_id, conversation_id)
        if state is None:
            # A call built outside prepare_tool_call: its delivery is unknown.
            return PreparedToolResult(result, session_id, None)

        prepared: Any = result
        delivered = False
        if state.output_instructions:
            prepared, delivered = mirror_instructions_into_structured_content(
                prepared, conversation_id
            )
        if state.minted:
            injected = _inject_prompt_back(prepared, conversation_id)
            if injected is not prepared:
                delivered = True
            prepared = injected
        return PreparedToolResult(
            prepared,
            session_id,
            None if state.minted and not delivered else conversation_id,
        )

    # --- internals -----------------------------------------------------------

    def _base_event(
        self,
        event_type: str,
        distinct_id: Optional[str],
        session_id: Optional[str],
        conversation_id: Optional[str],
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
            # Pass the value from prepare_tool_result: it drops a newly minted
            # handle that never reached the client.
            "conversation_id": conversation_id,
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

    def _is_sdk_virtual_tool(self, tool: Any) -> bool:
        """Whether this tool is one of the SDK's own descriptors, which carry
        their intent in their own arguments and so never get ``context``
        injected.

        Matched on the description, not the name: a host may own a tool called
        ``get_more_tools``, and a host re-preparing an already-prepared list
        hands our descriptor straight back, so both arrive under the same name.
        The description is ours and, unlike the schema, survives the
        model-injection pass."""
        name = _tool_name(tool)
        if name == self._missing_capability_tool_name:
            expected = build_report_missing_descriptor(name)
        elif self._collect_feedback is not None and name == self._feedback_tool_name:
            expected = get_feedback_tool_descriptor(self._collect_feedback)
        else:
            return False
        return _tool_description(tool) == expected["description"]

    def _inject_context(self, tool: Any, description: Optional[str]) -> Any:
        if isinstance(tool, dict):
            name = tool.get("name", "unknown")
            if self._is_sdk_virtual_tool(tool):
                return tool
            new_schema = add_context_parameter_to_schema(
                tool.get("inputSchema"), name, description
            )
            return {**tool, "inputSchema": new_schema}

        name = getattr(tool, "name", "unknown")
        if self._is_sdk_virtual_tool(tool):
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
        if not is_capture_model_enabled(self._capture_model):
            self._model_parameter_injected = {}
            return tools

        ownership: Dict[str, bool] = {}
        for tool in tools:
            name = _tool_name(tool)
            if name is None:
                continue
            can_inject = can_inject_model_parameter(_tool_schema(tool))
            ownership[name] = ownership.get(name, True) and can_inject
        prepared = [
            self._inject_model(tool, ownership)
            if ownership.get(_tool_name(tool) or "", True)
            else tool
            for tool in tools
        ]
        self._model_parameter_injected = ownership
        return prepared

    def _inject_model(self, tool: Any, ownership: Dict[str, bool]) -> Any:
        name = _tool_name(tool) or "unknown"

        schema = _tool_schema(tool)
        new_schema = add_model_parameter_to_schema(
            schema, name, get_model_description(self._capture_model)
        )
        if isinstance(tool, dict):
            return {**tool, "inputSchema": new_schema}
        try:
            prepared = copy.copy(tool)
            if prepared is tool:
                ownership[name] = False
                return tool
            if hasattr(prepared, "input_schema"):
                prepared.input_schema = new_schema
            else:
                prepared.inputSchema = new_schema
            return prepared
        except Exception:  # noqa: BLE001 - read-only descriptors fail closed
            ownership[name] = False
        return tool

    def _inject_conversation(
        self, tools: List[Any], ownership: Dict[str, _ConversationOwnership]
    ) -> List[Any]:
        if not self._enable_conversation_id:
            self._conversation_ownership = {}
            return tools
        prepared = []
        for tool in tools:
            name = _tool_name(tool)
            owned = ownership.get(name) if name is not None else None
            if name is None or owned is None or owned == _NOT_OWNED:
                prepared.append(tool)
                continue
            injected, ownership[name] = _inject_conversation_fields(tool, name, owned)
            prepared.append(injected)
        self._conversation_ownership = ownership
        return prepared


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


def _strip_conversation_id(args: Optional[JsonRecord]) -> Optional[JsonRecord]:
    if not args or "conversation_id" not in args:
        return args
    return {k: v for k, v in args.items() if k != "conversation_id"}


def _inject_conversation_fields(
    tool: Any, name: str, owned: _ConversationOwnership
) -> Tuple[Any, _ConversationOwnership]:
    """A copy of ``tool`` carrying the owned conversation fields, and the
    ownership that actually landed. Read-only descriptors fail closed."""
    injected = _copy_tool(tool)
    if injected is None:
        return tool, _NOT_OWNED
    try:
        if owned.conversation_id:
            _set_tool_schema(
                injected, add_conversation_id_to_schema(_tool_schema(tool), name)
            )
    except Exception:  # noqa: BLE001
        return tool, _NOT_OWNED
    if not owned.output_instructions:
        return injected, owned
    if isinstance(injected, dict):
        injected["outputSchema"] = declare_output_instructions(injected["outputSchema"])
        return injected, owned
    declared = add_instructions_to_output_schema(injected)
    return injected, _ConversationOwnership(owned.conversation_id, declared)


def _conversation_ownership_of(tool: Any) -> _ConversationOwnership:
    return _ConversationOwnership(
        conversation_id=can_inject_conversation_id(_tool_schema(tool)),
        output_instructions=can_declare_output_instructions(tool_output_schema(tool)),
    )


def _collect_conversation_ownership(
    tools: List[Any],
) -> Dict[str, _ConversationOwnership]:
    """Ownership per tool name. Two tools sharing a name fail closed: the SDK
    cannot tell which one a call targets."""
    ownership: Dict[str, _ConversationOwnership] = {}
    for tool in tools:
        name = _tool_name(tool)
        if name is None:
            continue
        found = _conversation_ownership_of(tool)
        current = ownership.get(name)
        if current is not None:
            found = _ConversationOwnership(
                current.conversation_id and found.conversation_id,
                current.output_instructions and found.output_instructions,
            )
        ownership[name] = found
    return ownership


def _inject_prompt_back(result: Any, conversation_id: str) -> Any:
    """Append the handle to a dict or ``CallToolResult`` result's ``content``,
    including a ``CallToolResult`` inside an MCP SDK 1.x ``ServerResult``.
    Returns ``result`` itself when there is no content list to append to."""
    if isinstance(result, dict):
        return inject_prompt_back(result, conversation_id)
    target = getattr(result, "root", result)
    content = getattr(target, "content", None)
    copy_model = getattr(target, "model_copy", None)
    if not isinstance(content, list) or not callable(copy_model):
        return result
    # A model result means the MCP SDK is installed; it stays a peer dependency.
    import mcp.types as mcp_types  # noqa: PLC0415

    block = mcp_types.TextContent(
        type="text", text=build_prompt_back(conversation_id)["text"]
    )
    try:
        updated = copy_model(update={"content": [*content, block]})
        if target is result:
            return updated
        return result.model_copy(update={"root": updated})
    except Exception:  # noqa: BLE001 - never let delivery break the tool path
        return result


def _copy_tool(tool: Any) -> Optional[Any]:
    """A shallow copy of ``tool``, or ``None`` when it cannot be copied."""
    if isinstance(tool, dict):
        return dict(tool)
    try:
        copied = copy.copy(tool)
    except Exception:  # noqa: BLE001
        return None
    return None if copied is tool else copied


def _set_tool_schema(tool: Any, schema: Any) -> None:
    if isinstance(tool, dict):
        tool["inputSchema"] = schema
    elif hasattr(tool, "input_schema"):
        tool.input_schema = schema
    else:
        tool.inputSchema = schema


def _tool_description(tool: Any) -> Any:
    """A tool's description, whether it is a dict or an SDK model."""
    if isinstance(tool, dict):
        return tool.get("description")
    return getattr(tool, "description", None)


def _tool_name(tool: Any) -> Optional[str]:
    if isinstance(tool, dict):
        return tool.get("name")
    return getattr(tool, "name", None)


def _find_tool(prepared: List[Any], name: str) -> Optional[Any]:
    """The listed tool using ``name``, or ``None``. One pass answers both "is
    the name taken" and "is the tool holding it ours"."""
    return next((tool for tool in prepared if _tool_name(tool) == name), None)


def _tool_schema(tool: Any) -> Optional[Dict[str, Any]]:
    if isinstance(tool, dict):
        schema = tool.get("inputSchema")
    else:
        schema = getattr(tool, "input_schema", None)
        if schema is None:
            schema = getattr(tool, "inputSchema", None)
    return schema if isinstance(schema, dict) else None
