import json
import logging
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Union

from agents.tracing import Span, Trace
from agents.tracing.processor_interface import TracingProcessor
from agents.tracing.span_data import (
    AgentSpanData,
    CustomSpanData,
    FunctionSpanData,
    GenerationSpanData,
    GuardrailSpanData,
    HandoffSpanData,
    MCPListToolsSpanData,
    ResponseSpanData,
    SpeechGroupSpanData,
    SpeechSpanData,
    TranscriptionSpanData,
)

from posthog import setup
from posthog.client import Client

log = logging.getLogger("posthog")


def _safe_json(obj: Any) -> Any:
    """Safely convert object to JSON-serializable format."""
    if obj is None:
        return None
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


def _parse_iso_timestamp(iso_str: Optional[str]) -> Optional[float]:
    """Parse ISO timestamp to Unix timestamp."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, AttributeError):
        return None


class PostHogTracingProcessor(TracingProcessor):
    """
    A tracing processor that sends OpenAI Agents SDK traces to PostHog.

    This processor implements the TracingProcessor interface from the OpenAI Agents SDK
    and maps agent traces, spans, and generations to PostHog's LLM analytics events.

    Example:
        ```python
        from agents import Agent, Runner
        from agents.tracing import add_trace_processor
        from posthog.ai.openai_agents import PostHogTracingProcessor

        # Create and register the processor
        processor = PostHogTracingProcessor(
            distinct_id="user@example.com",
            privacy_mode=False,
        )
        add_trace_processor(processor)

        # Run agents as normal - traces automatically sent to PostHog
        agent = Agent(name="Assistant", instructions="You are helpful.")
        result = Runner.run_sync(agent, "Hello!")
        ```
    """

    def __init__(
        self,
        client: Optional[Client] = None,
        distinct_id: Optional[Union[str, Callable[[Trace], Optional[str]]]] = None,
        privacy_mode: bool = False,
        groups: Optional[Dict[str, Any]] = None,
        properties: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize the PostHog tracing processor.

        Args:
            client: Optional PostHog client instance. If not provided, uses the default client.
            distinct_id: Either a string distinct ID or a callable that takes a Trace
                and returns a distinct ID. If not provided, uses the trace_id.
            privacy_mode: If True, redacts input/output content from events.
            groups: Optional PostHog groups to associate with all events.
            properties: Optional additional properties to include with all events.
        """
        self._client = client or setup()
        self._distinct_id = distinct_id
        self._privacy_mode = privacy_mode
        self._groups = groups or {}
        self._properties = properties or {}

        # Track span start times for latency calculation
        self._span_start_times: Dict[str, float] = {}

        # Track trace metadata for associating with spans
        self._trace_metadata: Dict[str, Dict[str, Any]] = {}

    def _get_distinct_id(self, trace: Optional[Trace]) -> str:
        """Resolve the distinct ID for a trace."""
        if callable(self._distinct_id):
            if trace:
                result = self._distinct_id(trace)
                if result:
                    return str(result)
            return trace.trace_id if trace else "unknown"
        elif self._distinct_id:
            return str(self._distinct_id)
        elif trace:
            return trace.trace_id
        return "unknown"

    def _with_privacy_mode(self, value: Any) -> Any:
        """Apply privacy mode redaction if enabled."""
        if self._privacy_mode or (
            hasattr(self._client, "privacy_mode") and self._client.privacy_mode
        ):
            return None
        return value

    def _get_group_id(self, trace_id: str) -> Optional[str]:
        """Get the group_id for a trace from stored metadata."""
        if trace_id in self._trace_metadata:
            return self._trace_metadata[trace_id].get("group_id")
        return None

    def _capture_event(
        self,
        event: str,
        properties: Dict[str, Any],
        distinct_id: Optional[str] = None,
    ) -> None:
        """Capture an event to PostHog with error handling."""
        try:
            if not hasattr(self._client, "capture") or not callable(self._client.capture):
                return

            final_distinct_id = distinct_id or "unknown"
            final_properties = {
                **properties,
                **self._properties,
            }

            # Don't process person profile if no distinct_id
            if distinct_id is None:
                final_properties["$process_person_profile"] = False

            self._client.capture(
                distinct_id=final_distinct_id,
                event=event,
                properties=final_properties,
                groups=self._groups,
            )
        except Exception as e:
            log.debug(f"Failed to capture PostHog event: {e}")

    def on_trace_start(self, trace: Trace) -> None:
        """Called when a new trace begins."""
        try:
            trace_id = trace.trace_id
            trace_name = trace.name
            group_id = getattr(trace, "group_id", None)
            metadata = getattr(trace, "metadata", None)

            # Store trace metadata for later (used by spans)
            self._trace_metadata[trace_id] = {
                "name": trace_name,
                "group_id": group_id,
                "metadata": metadata,
            }

            distinct_id = self._get_distinct_id(trace)

            properties = {
                "$ai_trace_id": trace_id,
                "$ai_trace_name": trace_name,
                "$ai_provider": "openai_agents",
            }

            # Include group_id for linking related traces (e.g., conversation threads)
            if group_id:
                properties["$ai_group_id"] = group_id

            # Include trace metadata if present
            if metadata:
                properties["$ai_trace_metadata"] = _safe_json(metadata)

            self._capture_event(
                event="$ai_trace",
                distinct_id=distinct_id,
                properties=properties,
            )
        except Exception as e:
            log.debug(f"Error in on_trace_start: {e}")

    def on_trace_end(self, trace: Trace) -> None:
        """Called when a trace completes."""
        try:
            trace_id = trace.trace_id

            # Clean up stored metadata
            self._trace_metadata.pop(trace_id, None)
        except Exception as e:
            log.debug(f"Error in on_trace_end: {e}")

    def on_span_start(self, span: Span[Any]) -> None:
        """Called when a new span begins."""
        try:
            span_id = span.span_id
            self._span_start_times[span_id] = time.time()
        except Exception as e:
            log.debug(f"Error in on_span_start: {e}")

    def on_span_end(self, span: Span[Any]) -> None:
        """Called when a span completes."""
        try:
            span_id = span.span_id
            trace_id = span.trace_id
            parent_id = span.parent_id
            span_data = span.span_data

            # Calculate latency
            start_time = self._span_start_times.pop(span_id, None)
            if start_time:
                latency = time.time() - start_time
            else:
                # Fall back to parsing timestamps
                started = _parse_iso_timestamp(span.started_at)
                ended = _parse_iso_timestamp(span.ended_at)
                latency = (ended - started) if (started and ended) else 0

            # Get distinct ID from trace metadata or default
            distinct_id = self._get_distinct_id(None)

            # Get group_id from trace metadata for linking
            group_id = self._get_group_id(trace_id)

            # Get error info if present
            error_info = span.error
            error_properties = {}
            if error_info:
                error_properties = {
                    "$ai_is_error": True,
                    "$ai_error": error_info.get("message", str(error_info)),
                }

            # Dispatch based on span data type
            if isinstance(span_data, GenerationSpanData):
                self._handle_generation_span(
                    span_data, trace_id, span_id, parent_id, latency, distinct_id, group_id, error_properties
                )
            elif isinstance(span_data, FunctionSpanData):
                self._handle_function_span(
                    span_data, trace_id, span_id, parent_id, latency, distinct_id, group_id, error_properties
                )
            elif isinstance(span_data, AgentSpanData):
                self._handle_agent_span(
                    span_data, trace_id, span_id, parent_id, latency, distinct_id, group_id, error_properties
                )
            elif isinstance(span_data, HandoffSpanData):
                self._handle_handoff_span(
                    span_data, trace_id, span_id, parent_id, latency, distinct_id, group_id, error_properties
                )
            elif isinstance(span_data, GuardrailSpanData):
                self._handle_guardrail_span(
                    span_data, trace_id, span_id, parent_id, latency, distinct_id, group_id, error_properties
                )
            elif isinstance(span_data, ResponseSpanData):
                self._handle_response_span(
                    span_data, trace_id, span_id, parent_id, latency, distinct_id, group_id, error_properties
                )
            elif isinstance(span_data, CustomSpanData):
                self._handle_custom_span(
                    span_data, trace_id, span_id, parent_id, latency, distinct_id, group_id, error_properties
                )
            elif isinstance(span_data, (TranscriptionSpanData, SpeechSpanData, SpeechGroupSpanData)):
                self._handle_audio_span(
                    span_data, trace_id, span_id, parent_id, latency, distinct_id, group_id, error_properties
                )
            elif isinstance(span_data, MCPListToolsSpanData):
                self._handle_mcp_span(
                    span_data, trace_id, span_id, parent_id, latency, distinct_id, group_id, error_properties
                )
            else:
                # Unknown span type - capture as generic span
                self._handle_generic_span(
                    span_data, trace_id, span_id, parent_id, latency, distinct_id, group_id, error_properties
                )

        except Exception as e:
            log.debug(f"Error in on_span_end: {e}")

    def _handle_generation_span(
        self,
        span_data: GenerationSpanData,
        trace_id: str,
        span_id: str,
        parent_id: Optional[str],
        latency: float,
        distinct_id: str,
        group_id: Optional[str],
        error_properties: Dict[str, Any],
    ) -> None:
        """Handle LLM generation spans - maps to $ai_generation event."""
        # Extract token usage
        usage = span_data.usage or {}
        input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens", 0)
        output_tokens = usage.get("output_tokens") or usage.get("completion_tokens", 0)

        # Extract model config parameters
        model_config = span_data.model_config or {}
        model_params = {}
        for param in ["temperature", "max_tokens", "top_p", "frequency_penalty", "presence_penalty"]:
            if param in model_config:
                model_params[param] = model_config[param]

        properties = {
            "$ai_trace_id": trace_id,
            "$ai_span_id": span_id,
            "$ai_parent_id": parent_id,
            "$ai_provider": "openai",
            "$ai_model": span_data.model,
            "$ai_model_parameters": model_params if model_params else None,
            "$ai_input": self._with_privacy_mode(_safe_json(span_data.input)),
            "$ai_output_choices": self._with_privacy_mode(_safe_json(span_data.output)),
            "$ai_input_tokens": input_tokens,
            "$ai_output_tokens": output_tokens,
            "$ai_latency": latency,
            **error_properties,
        }

        # Include group_id for linking related traces
        if group_id:
            properties["$ai_group_id"] = group_id

        # Add optional token fields if present
        if usage.get("reasoning_tokens"):
            properties["$ai_reasoning_tokens"] = usage["reasoning_tokens"]
        if usage.get("cache_read_input_tokens"):
            properties["$ai_cache_read_input_tokens"] = usage["cache_read_input_tokens"]
        if usage.get("cache_creation_input_tokens"):
            properties["$ai_cache_creation_input_tokens"] = usage["cache_creation_input_tokens"]

        self._capture_event("$ai_generation", properties, distinct_id)

    def _handle_function_span(
        self,
        span_data: FunctionSpanData,
        trace_id: str,
        span_id: str,
        parent_id: Optional[str],
        latency: float,
        distinct_id: str,
        group_id: Optional[str],
        error_properties: Dict[str, Any],
    ) -> None:
        """Handle function/tool call spans - maps to $ai_span event."""
        properties = {
            "$ai_trace_id": trace_id,
            "$ai_span_id": span_id,
            "$ai_parent_id": parent_id,
            "$ai_span_name": span_data.name,
            "$ai_span_type": "tool",
            "$ai_provider": "openai_agents",
            "$ai_input_state": self._with_privacy_mode(_safe_json(span_data.input)),
            "$ai_output_state": self._with_privacy_mode(_safe_json(span_data.output)),
            "$ai_latency": latency,
            **error_properties,
        }

        # Include group_id for linking related traces
        if group_id:
            properties["$ai_group_id"] = group_id

        # Add MCP data if present
        if span_data.mcp_data:
            properties["$ai_mcp_data"] = _safe_json(span_data.mcp_data)

        self._capture_event("$ai_span", properties, distinct_id)

    def _handle_agent_span(
        self,
        span_data: AgentSpanData,
        trace_id: str,
        span_id: str,
        parent_id: Optional[str],
        latency: float,
        distinct_id: str,
        group_id: Optional[str],
        error_properties: Dict[str, Any],
    ) -> None:
        """Handle agent execution spans - maps to $ai_span event."""
        properties = {
            "$ai_trace_id": trace_id,
            "$ai_span_id": span_id,
            "$ai_parent_id": parent_id,
            "$ai_span_name": span_data.name,
            "$ai_span_type": "agent",
            "$ai_provider": "openai_agents",
            "$ai_latency": latency,
            **error_properties,
        }

        # Include group_id for linking related traces
        if group_id:
            properties["$ai_group_id"] = group_id

        # Add agent-specific metadata
        if span_data.handoffs:
            properties["$ai_agent_handoffs"] = span_data.handoffs
        if span_data.tools:
            properties["$ai_agent_tools"] = span_data.tools
        if span_data.output_type:
            properties["$ai_agent_output_type"] = span_data.output_type

        self._capture_event("$ai_span", properties, distinct_id)

    def _handle_handoff_span(
        self,
        span_data: HandoffSpanData,
        trace_id: str,
        span_id: str,
        parent_id: Optional[str],
        latency: float,
        distinct_id: str,
        group_id: Optional[str],
        error_properties: Dict[str, Any],
    ) -> None:
        """Handle agent handoff spans - maps to $ai_span event."""
        properties = {
            "$ai_trace_id": trace_id,
            "$ai_span_id": span_id,
            "$ai_parent_id": parent_id,
            "$ai_span_name": f"{span_data.from_agent} -> {span_data.to_agent}",
            "$ai_span_type": "handoff",
            "$ai_provider": "openai_agents",
            "$ai_handoff_from_agent": span_data.from_agent,
            "$ai_handoff_to_agent": span_data.to_agent,
            "$ai_latency": latency,
            **error_properties,
        }

        # Include group_id for linking related traces
        if group_id:
            properties["$ai_group_id"] = group_id

        self._capture_event("$ai_span", properties, distinct_id)

    def _handle_guardrail_span(
        self,
        span_data: GuardrailSpanData,
        trace_id: str,
        span_id: str,
        parent_id: Optional[str],
        latency: float,
        distinct_id: str,
        group_id: Optional[str],
        error_properties: Dict[str, Any],
    ) -> None:
        """Handle guardrail execution spans - maps to $ai_span event."""
        properties = {
            "$ai_trace_id": trace_id,
            "$ai_span_id": span_id,
            "$ai_parent_id": parent_id,
            "$ai_span_name": span_data.name,
            "$ai_span_type": "guardrail",
            "$ai_provider": "openai_agents",
            "$ai_guardrail_triggered": span_data.triggered,
            "$ai_latency": latency,
            **error_properties,
        }

        # Include group_id for linking related traces
        if group_id:
            properties["$ai_group_id"] = group_id

        self._capture_event("$ai_span", properties, distinct_id)

    def _handle_response_span(
        self,
        span_data: ResponseSpanData,
        trace_id: str,
        span_id: str,
        parent_id: Optional[str],
        latency: float,
        distinct_id: str,
        group_id: Optional[str],
        error_properties: Dict[str, Any],
    ) -> None:
        """Handle OpenAI Response API spans - maps to $ai_generation event."""
        response = span_data.response
        response_id = response.id if response else None

        # Try to extract usage from response
        usage = getattr(response, "usage", None) if response else None
        input_tokens = 0
        output_tokens = 0
        if usage:
            input_tokens = getattr(usage, "input_tokens", 0) or 0
            output_tokens = getattr(usage, "output_tokens", 0) or 0

        # Try to extract model from response
        model = getattr(response, "model", None) if response else None

        properties = {
            "$ai_trace_id": trace_id,
            "$ai_span_id": span_id,
            "$ai_parent_id": parent_id,
            "$ai_provider": "openai",
            "$ai_model": model,
            "$ai_response_id": response_id,
            "$ai_input": self._with_privacy_mode(_safe_json(span_data.input)),
            "$ai_input_tokens": input_tokens,
            "$ai_output_tokens": output_tokens,
            "$ai_latency": latency,
            **error_properties,
        }

        # Include group_id for linking related traces
        if group_id:
            properties["$ai_group_id"] = group_id

        self._capture_event("$ai_generation", properties, distinct_id)

    def _handle_custom_span(
        self,
        span_data: CustomSpanData,
        trace_id: str,
        span_id: str,
        parent_id: Optional[str],
        latency: float,
        distinct_id: str,
        group_id: Optional[str],
        error_properties: Dict[str, Any],
    ) -> None:
        """Handle custom user-defined spans - maps to $ai_span event."""
        properties = {
            "$ai_trace_id": trace_id,
            "$ai_span_id": span_id,
            "$ai_parent_id": parent_id,
            "$ai_span_name": span_data.name,
            "$ai_span_type": "custom",
            "$ai_provider": "openai_agents",
            "$ai_custom_data": self._with_privacy_mode(_safe_json(span_data.data)),
            "$ai_latency": latency,
            **error_properties,
        }

        # Include group_id for linking related traces
        if group_id:
            properties["$ai_group_id"] = group_id

        self._capture_event("$ai_span", properties, distinct_id)

    def _handle_audio_span(
        self,
        span_data: Union[TranscriptionSpanData, SpeechSpanData, SpeechGroupSpanData],
        trace_id: str,
        span_id: str,
        parent_id: Optional[str],
        latency: float,
        distinct_id: str,
        group_id: Optional[str],
        error_properties: Dict[str, Any],
    ) -> None:
        """Handle audio-related spans (transcription, speech) - maps to $ai_span event."""
        span_type = span_data.type  # "transcription", "speech", or "speech_group"

        properties = {
            "$ai_trace_id": trace_id,
            "$ai_span_id": span_id,
            "$ai_parent_id": parent_id,
            "$ai_span_name": span_type,
            "$ai_span_type": span_type,
            "$ai_provider": "openai_agents",
            "$ai_latency": latency,
            **error_properties,
        }

        # Include group_id for linking related traces
        if group_id:
            properties["$ai_group_id"] = group_id

        # Add model info if available
        if hasattr(span_data, "model") and span_data.model:
            properties["$ai_model"] = span_data.model

        # Don't include audio data (base64) - just metadata
        if hasattr(span_data, "output") and isinstance(span_data.output, str):
            # For transcription, output is the text
            properties["$ai_output_state"] = self._with_privacy_mode(span_data.output)

        self._capture_event("$ai_span", properties, distinct_id)

    def _handle_mcp_span(
        self,
        span_data: MCPListToolsSpanData,
        trace_id: str,
        span_id: str,
        parent_id: Optional[str],
        latency: float,
        distinct_id: str,
        group_id: Optional[str],
        error_properties: Dict[str, Any],
    ) -> None:
        """Handle MCP (Model Context Protocol) spans - maps to $ai_span event."""
        properties = {
            "$ai_trace_id": trace_id,
            "$ai_span_id": span_id,
            "$ai_parent_id": parent_id,
            "$ai_span_name": f"mcp:{span_data.server}",
            "$ai_span_type": "mcp_tools",
            "$ai_provider": "openai_agents",
            "$ai_mcp_server": span_data.server,
            "$ai_mcp_tools": span_data.result,
            "$ai_latency": latency,
            **error_properties,
        }

        # Include group_id for linking related traces
        if group_id:
            properties["$ai_group_id"] = group_id

        self._capture_event("$ai_span", properties, distinct_id)

    def _handle_generic_span(
        self,
        span_data: Any,
        trace_id: str,
        span_id: str,
        parent_id: Optional[str],
        latency: float,
        distinct_id: str,
        group_id: Optional[str],
        error_properties: Dict[str, Any],
    ) -> None:
        """Handle unknown span types - maps to $ai_span event."""
        span_type = getattr(span_data, "type", "unknown")

        properties = {
            "$ai_trace_id": trace_id,
            "$ai_span_id": span_id,
            "$ai_parent_id": parent_id,
            "$ai_span_name": span_type,
            "$ai_span_type": span_type,
            "$ai_provider": "openai_agents",
            "$ai_latency": latency,
            **error_properties,
        }

        # Include group_id for linking related traces
        if group_id:
            properties["$ai_group_id"] = group_id

        # Try to export span data
        if hasattr(span_data, "export"):
            try:
                exported = span_data.export()
                properties["$ai_span_data"] = _safe_json(exported)
            except Exception:
                pass

        self._capture_event("$ai_span", properties, distinct_id)

    def shutdown(self) -> None:
        """Clean up resources when the application stops."""
        try:
            self._span_start_times.clear()
            self._trace_metadata.clear()

            # Flush the PostHog client if possible
            if hasattr(self._client, "flush") and callable(self._client.flush):
                self._client.flush()
        except Exception as e:
            log.debug(f"Error in shutdown: {e}")

    def force_flush(self) -> None:
        """Force immediate processing of any queued events."""
        try:
            if hasattr(self._client, "flush") and callable(self._client.flush):
                self._client.flush()
        except Exception as e:
            log.debug(f"Error in force_flush: {e}")
