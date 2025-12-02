"""
PostHog SpanExporter for OpenTelemetry.

Translates OpenTelemetry spans (using GenAI semantic conventions) into PostHog AI events.
This enables any OTel-instrumented AI framework (Pydantic AI, LlamaIndex, etc.) to send
telemetry to PostHog.
"""

import json
import logging
from typing import Any, Dict, Optional, Sequence, Union

from posthog.client import Client as PostHogClient

try:
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
    from opentelemetry.trace import StatusCode

    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False
    # Define stub types for type hints when OTel is not installed
    ReadableSpan = Any  # type: ignore
    SpanExporter = object  # type: ignore
    SpanExportResult = Any  # type: ignore
    StatusCode = Any  # type: ignore

logger = logging.getLogger(__name__)


# OTel GenAI semantic convention attribute names
# See: https://opentelemetry.io/docs/specs/semconv/gen-ai/
class GenAIAttributes:
    # Operation
    OPERATION_NAME = "gen_ai.operation.name"

    # Request attributes
    REQUEST_MODEL = "gen_ai.request.model"
    SYSTEM = "gen_ai.system"
    PROVIDER_NAME = "gen_ai.provider_name"  # Alternative to gen_ai.system

    # Response attributes
    RESPONSE_MODEL = "gen_ai.response.model"
    RESPONSE_ID = "gen_ai.response.id"
    FINISH_REASONS = "gen_ai.response.finish_reasons"

    # Usage attributes
    INPUT_TOKENS = "gen_ai.usage.input_tokens"
    OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

    # Message content (when captured)
    INPUT_MESSAGES = "gen_ai.input.messages"
    OUTPUT_MESSAGES = "gen_ai.output.messages"
    SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"

    # Pydantic AI specific
    AGENT_NAME = "gen_ai.agent.name"
    AGENT_NAME_LEGACY = "agent_name"

    # Tool attributes
    TOOL_NAME = "gen_ai.tool.name"
    TOOL_CALL_ID = "gen_ai.tool.call.id"
    TOOL_ARGUMENTS = "gen_ai.tool.call.arguments"
    TOOL_RESULT = "gen_ai.tool.call.result"

    # Model parameters
    TEMPERATURE = "gen_ai.request.temperature"
    TOP_P = "gen_ai.request.top_p"
    MAX_TOKENS = "gen_ai.request.max_tokens"
    FREQUENCY_PENALTY = "gen_ai.request.frequency_penalty"
    PRESENCE_PENALTY = "gen_ai.request.presence_penalty"
    SEED = "gen_ai.request.seed"

    # Server info
    SERVER_ADDRESS = "server.address"
    SERVER_PORT = "server.port"


class PostHogSpanExporter(SpanExporter if OTEL_AVAILABLE else object):
    """
    OpenTelemetry SpanExporter that sends AI/LLM spans to PostHog.

    Translates OTel GenAI semantic convention spans into PostHog AI events:
    - Model request spans → $ai_generation
    - Agent run spans → $ai_trace
    - Tool execution spans → $ai_span

    Usage:
        from posthog import Posthog
        from posthog.ai.otel import PostHogSpanExporter
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        posthog = Posthog(api_key="...", host="...")
        exporter = PostHogSpanExporter(posthog, distinct_id="user_123")

        provider = TracerProvider()
        provider.add_span_processor(BatchSpanProcessor(exporter))
    """

    def __init__(
        self,
        client: PostHogClient,
        distinct_id: Optional[str] = None,
        privacy_mode: bool = False,
        properties: Optional[Dict[str, Any]] = None,
        groups: Optional[Dict[str, Any]] = None,
        debug: bool = False,
    ):
        """
        Initialize the PostHog span exporter.

        Args:
            client: PostHog client instance
            distinct_id: Default distinct ID for events (can be overridden per-span)
            privacy_mode: If True, redact message content from events
            properties: Additional properties to include in all events
            groups: PostHog groups for all events
            debug: Enable debug logging
        """
        if not OTEL_AVAILABLE:
            raise ImportError(
                "OpenTelemetry SDK is required for PostHogSpanExporter. "
                "Install it with: pip install opentelemetry-sdk"
            )

        self._client = client
        self._distinct_id = distinct_id
        self._privacy_mode = privacy_mode or getattr(client, "privacy_mode", False)
        self._properties = properties or {}
        self._groups = groups
        self._debug = debug

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """
        Export spans to PostHog.

        Translates each span into the appropriate PostHog event type.
        """
        for span in spans:
            try:
                event = self._span_to_event(span)
                if event:
                    distinct_id = self._get_distinct_id(span)

                    if self._debug:
                        logger.debug(
                            f"Exporting span '{span.name}' as {event['name']} "
                            f"with distinct_id={distinct_id}"
                        )

                    capture_kwargs: Dict[str, Any] = {
                        "distinct_id": distinct_id,
                        "event": event["name"],
                        "properties": event["properties"],
                    }

                    if self._groups:
                        capture_kwargs["groups"] = self._groups

                    self._client.capture(**capture_kwargs)

            except Exception as e:
                logger.warning(f"Failed to export span '{span.name}': {e}")
                if self._debug:
                    logger.exception("Full exception:")

        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        """Shutdown the exporter."""
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Force flush any buffered spans."""
        return True

    def _get_distinct_id(self, span: ReadableSpan) -> str:
        """Get distinct ID for a span, with fallback to trace ID."""
        attrs = dict(span.attributes or {})

        # Check for custom distinct_id attribute
        distinct_id = attrs.get("posthog.distinct_id")
        if distinct_id:
            return str(distinct_id)

        # Use configured default
        if self._distinct_id:
            return self._distinct_id

        # Fall back to trace ID
        return format(span.context.trace_id, "032x")

    def _span_to_event(self, span: ReadableSpan) -> Optional[Dict[str, Any]]:
        """
        Convert an OTel span to a PostHog event.

        Returns None for spans that shouldn't be exported.
        """
        attrs = dict(span.attributes or {})
        span_name = span.name

        # Calculate latency in seconds
        latency = (
            (span.end_time - span.start_time) / 1e9
            if span.end_time and span.start_time
            else 0
        )

        # Format trace ID as UUID (with dashes) for PostHog compatibility
        trace_id = self._format_trace_id_as_uuid(span.context.trace_id)
        # Span IDs remain as hex (no dashes needed)
        span_id = format(span.context.span_id, "016x")
        parent_span_id = (
            format(span.parent.span_id, "016x") if span.parent else None
        )

        # Check for error status
        is_error = span.status.status_code == StatusCode.ERROR if span.status else False
        error_message = span.status.description if is_error and span.status else None

        # Model request span → $ai_generation
        if self._is_generation_span(span_name, attrs):
            return self._create_generation_event(
                span, attrs, trace_id, span_id, parent_span_id, latency, is_error, error_message
            )

        # Agent run span → skip (PostHog UI auto-creates trace wrapper from generation events)
        # The $ai_trace_id on generation events is sufficient for grouping
        if self._is_agent_span(span_name, attrs):
            return None  # Don't emit separate $ai_trace events

        # Tool execution span → $ai_span
        if self._is_tool_span(span_name, attrs):
            return self._create_tool_span_event(
                span, attrs, trace_id, span_id, parent_span_id, latency, is_error, error_message
            )

        # Generic span that might be part of AI workflow
        if self._is_ai_related_span(span_name, attrs):
            return self._create_span_event(
                span, attrs, trace_id, span_id, parent_span_id, latency, is_error, error_message
            )

        return None

    def _is_generation_span(self, span_name: str, attrs: Dict[str, Any]) -> bool:
        """Check if span represents an LLM generation/chat completion."""
        operation = attrs.get(GenAIAttributes.OPERATION_NAME, "")
        return (
            span_name.startswith("chat ")
            or operation == "chat"
            or attrs.get(GenAIAttributes.REQUEST_MODEL) is not None
        )

    def _is_agent_span(self, span_name: str, attrs: Dict[str, Any]) -> bool:
        """Check if span represents an agent run."""
        return span_name in ("agent run", "invoke_agent") or attrs.get(
            GenAIAttributes.AGENT_NAME
        )

    def _is_tool_span(self, span_name: str, attrs: Dict[str, Any]) -> bool:
        """Check if span represents a tool/function execution."""
        return (
            "tool" in span_name.lower()
            or "execute_tool" in span_name
            or attrs.get(GenAIAttributes.TOOL_NAME) is not None
        )

    def _is_ai_related_span(self, span_name: str, attrs: Dict[str, Any]) -> bool:
        """Check if span is AI-related based on attributes."""
        ai_attrs = [
            GenAIAttributes.SYSTEM,
            GenAIAttributes.PROVIDER_NAME,
            GenAIAttributes.REQUEST_MODEL,
            GenAIAttributes.AGENT_NAME,
        ]
        return any(attrs.get(attr) for attr in ai_attrs)

    def _get_generation_span_name(self, span_name: str, provider: str) -> str:
        """
        Derive a descriptive span name for generation events.

        Returns something like 'openai_chat_completions' based on provider.
        """
        # If span name already looks like a good identifier, use it
        if span_name and not span_name.startswith("chat "):
            # Clean up span name to be a good identifier
            clean_name = span_name.replace(" ", "_").replace("-", "_").lower()
            return clean_name

        # Otherwise derive from provider
        provider_lower = str(provider).lower() if provider else "unknown"
        return f"{provider_lower}_chat_completions"

    def _create_generation_event(
        self,
        span: ReadableSpan,
        attrs: Dict[str, Any],
        trace_id: str,
        span_id: str,
        parent_span_id: Optional[str],
        latency: float,
        is_error: bool,
        error_message: Optional[str],
    ) -> Dict[str, Any]:
        """Create a $ai_generation event from a model request span."""
        # Extract model and provider info
        model = attrs.get(GenAIAttributes.REQUEST_MODEL) or attrs.get(
            GenAIAttributes.RESPONSE_MODEL
        )
        provider = attrs.get(GenAIAttributes.SYSTEM) or attrs.get(
            GenAIAttributes.PROVIDER_NAME, "unknown"
        )

        # Extract token usage
        input_tokens = attrs.get(GenAIAttributes.INPUT_TOKENS)
        output_tokens = attrs.get(GenAIAttributes.OUTPUT_TOKENS)

        # Extract messages (respecting privacy mode)
        input_messages = None
        output_messages = None
        if not self._privacy_mode:
            input_messages = self._parse_json_attr(
                attrs.get(GenAIAttributes.INPUT_MESSAGES)
            )
            output_messages = self._parse_json_attr(
                attrs.get(GenAIAttributes.OUTPUT_MESSAGES)
            )

        # Build base URL from server info
        server_address = attrs.get(GenAIAttributes.SERVER_ADDRESS)
        server_port = attrs.get(GenAIAttributes.SERVER_PORT)
        base_url = None
        if server_address:
            base_url = f"https://{server_address}"
            if server_port:
                base_url = f"{base_url}:{server_port}"

        # Extract model parameters
        model_params = {}
        param_attrs = [
            (GenAIAttributes.TEMPERATURE, "temperature"),
            (GenAIAttributes.TOP_P, "top_p"),
            (GenAIAttributes.MAX_TOKENS, "max_tokens"),
            (GenAIAttributes.FREQUENCY_PENALTY, "frequency_penalty"),
            (GenAIAttributes.PRESENCE_PENALTY, "presence_penalty"),
            (GenAIAttributes.SEED, "seed"),
        ]
        for otel_attr, param_name in param_attrs:
            if otel_attr in attrs:
                model_params[param_name] = attrs[otel_attr]

        # Derive span name from span name or provider
        generation_span_name = self._get_generation_span_name(span.name, provider)

        # PostHog expects generation events to NOT have span_id/parent_id
        # The $ai_trace_id alone is sufficient for grouping
        properties: Dict[str, Any] = {
            "$ai_trace_id": trace_id,
            "$ai_span_name": generation_span_name,
            "$ai_model": model,
            "$ai_provider": provider,
            "$ai_latency": latency,
            "$ai_http_status": 500 if is_error else 200,
            "$ai_is_error": is_error,
            "$ai_framework": "opentelemetry",
            **self._properties,
        }

        if model_params:
            properties["$ai_model_parameters"] = model_params

        if input_tokens is not None:
            properties["$ai_input_tokens"] = input_tokens

        if output_tokens is not None:
            properties["$ai_output_tokens"] = output_tokens

        if input_messages is not None:
            properties["$ai_input"] = input_messages

        if output_messages is not None:
            properties["$ai_output_choices"] = output_messages

        if base_url:
            properties["$ai_base_url"] = base_url

        if is_error and error_message:
            properties["$ai_error"] = error_message

        # Handle distinct_id for person profile processing
        if not self._distinct_id and not attrs.get("posthog.distinct_id"):
            properties["$process_person_profile"] = False

        return {"name": "$ai_generation", "properties": properties}

    def _create_trace_event(
        self,
        span: ReadableSpan,
        attrs: Dict[str, Any],
        trace_id: str,
        span_id: str,
        latency: float,
        is_error: bool,
        error_message: Optional[str],
    ) -> Dict[str, Any]:
        """Create a $ai_trace event from an agent run span."""
        agent_name = attrs.get(GenAIAttributes.AGENT_NAME) or attrs.get(
            GenAIAttributes.AGENT_NAME_LEGACY, "unknown"
        )

        properties: Dict[str, Any] = {
            "$ai_trace_id": trace_id,
            "$ai_span_id": span_id,
            "$ai_span_name": agent_name,
            "$ai_latency": latency,
            "$ai_is_error": is_error,
            "$ai_framework": "pydantic-ai",
            **self._properties,
        }

        if is_error and error_message:
            properties["$ai_error"] = error_message

        if not self._distinct_id and not attrs.get("posthog.distinct_id"):
            properties["$process_person_profile"] = False

        return {"name": "$ai_trace", "properties": properties}

    def _create_tool_span_event(
        self,
        span: ReadableSpan,
        attrs: Dict[str, Any],
        trace_id: str,
        span_id: str,
        parent_span_id: Optional[str],
        latency: float,
        is_error: bool,
        error_message: Optional[str],
    ) -> Dict[str, Any]:
        """Create a $ai_span event from a tool execution span."""
        tool_name = attrs.get(GenAIAttributes.TOOL_NAME, span.name)

        properties: Dict[str, Any] = {
            "$ai_trace_id": trace_id,
            "$ai_span_id": span_id,
            "$ai_span_name": tool_name,
            "$ai_latency": latency,
            "$ai_is_error": is_error,
            "$ai_framework": "pydantic-ai",
            **self._properties,
        }

        if parent_span_id:
            properties["$ai_parent_id"] = parent_span_id

        # Include tool arguments and result if not in privacy mode
        if not self._privacy_mode:
            tool_args = attrs.get(GenAIAttributes.TOOL_ARGUMENTS)
            if tool_args:
                properties["$ai_tool_arguments"] = self._parse_json_attr(tool_args)

            tool_result = attrs.get(GenAIAttributes.TOOL_RESULT)
            if tool_result:
                properties["$ai_tool_result"] = self._parse_json_attr(tool_result)

        if is_error and error_message:
            properties["$ai_error"] = error_message

        if not self._distinct_id and not attrs.get("posthog.distinct_id"):
            properties["$process_person_profile"] = False

        return {"name": "$ai_span", "properties": properties}

    def _create_span_event(
        self,
        span: ReadableSpan,
        attrs: Dict[str, Any],
        trace_id: str,
        span_id: str,
        parent_span_id: Optional[str],
        latency: float,
        is_error: bool,
        error_message: Optional[str],
    ) -> Dict[str, Any]:
        """Create a generic $ai_span event."""
        properties: Dict[str, Any] = {
            "$ai_trace_id": trace_id,
            "$ai_span_id": span_id,
            "$ai_span_name": span.name,
            "$ai_latency": latency,
            "$ai_is_error": is_error,
            "$ai_framework": "pydantic-ai",
            **self._properties,
        }

        if parent_span_id:
            properties["$ai_parent_id"] = parent_span_id

        if is_error and error_message:
            properties["$ai_error"] = error_message

        if not self._distinct_id and not attrs.get("posthog.distinct_id"):
            properties["$process_person_profile"] = False

        return {"name": "$ai_span", "properties": properties}

    def _parse_json_attr(
        self, value: Optional[Union[str, Any]]
    ) -> Optional[Any]:
        """Parse a JSON string attribute, returning the value as-is if already parsed."""
        if value is None:
            return None
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    def _format_trace_id_as_uuid(self, trace_id: int) -> str:
        """
        Convert an OTel trace ID (128-bit int) to UUID format with dashes.

        PostHog expects trace IDs in UUID format (e.g., 'a8f3d2c4-1247-4c40-8342-23d5e8d52584')
        but OTel uses 128-bit integers formatted as 32 hex chars without dashes.
        """
        hex_str = format(trace_id, "032x")
        # Insert dashes to form UUID format: 8-4-4-4-12
        return f"{hex_str[:8]}-{hex_str[8:12]}-{hex_str[12:16]}-{hex_str[16:20]}-{hex_str[20:]}"
