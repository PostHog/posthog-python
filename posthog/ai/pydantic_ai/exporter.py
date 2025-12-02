"""
Pydantic AI specific SpanExporter for PostHog.

This exporter wraps the generic PostHogSpanExporter and handles
Pydantic AI-specific transformations like message format normalization.
"""

from typing import Any, Dict, List, Optional, Sequence

try:
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False
    ReadableSpan = Any  # type: ignore
    SpanExporter = object  # type: ignore
    SpanExportResult = Any  # type: ignore

from posthog.ai.otel import PostHogSpanExporter
from posthog.client import Client as PostHogClient


class PydanticAISpanExporter(SpanExporter if OTEL_AVAILABLE else object):
    """
    SpanExporter for Pydantic AI that normalizes messages to OpenAI format.

    Pydantic AI uses its own message format with "parts":
        {"parts": [{"content": "...", "type": "text"}], "role": "user"}

    This exporter transforms that to the standard OpenAI format:
        {"content": "...", "role": "user"}

    This ensures consistent display in PostHog's LLM Analytics UI.
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
        if not OTEL_AVAILABLE:
            raise ImportError(
                "OpenTelemetry SDK is required. Install with: pip install opentelemetry-sdk"
            )

        # Wrap the generic PostHog exporter
        self._base_exporter = PostHogSpanExporter(
            client=client,
            distinct_id=distinct_id,
            privacy_mode=privacy_mode,
            properties=properties,
            groups=groups,
            debug=debug,
        )
        self._debug = debug

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Export spans after normalizing Pydantic AI message formats."""
        # Transform spans to normalize message format
        transformed_spans = [self._transform_span(span) for span in spans]
        return self._base_exporter.export(transformed_spans)

    def shutdown(self) -> None:
        """Shutdown the exporter."""
        self._base_exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Force flush any buffered spans."""
        return self._base_exporter.force_flush(timeout_millis)

    def _transform_span(self, span: ReadableSpan) -> ReadableSpan:
        """
        Transform a span's attributes to normalize Pydantic AI-specific formats.

        Handles:
        - Message format: Pydantic AI "parts" format → OpenAI format
        - Tool attributes: tool_arguments/tool_response → gen_ai.tool.call.arguments/result
        """
        import json

        attrs = dict(span.attributes or {})
        modified = False

        # Normalize input messages from Pydantic AI "parts" format
        input_msgs = attrs.get("gen_ai.input.messages")
        if input_msgs:
            normalized = self._normalize_messages(input_msgs)
            if normalized != input_msgs:
                attrs["gen_ai.input.messages"] = (
                    json.dumps(normalized)
                    if isinstance(normalized, list)
                    else normalized
                )
                modified = True

        # Normalize output messages from Pydantic AI "parts" format
        output_msgs = attrs.get("gen_ai.output.messages")
        if output_msgs:
            normalized = self._normalize_messages(output_msgs)
            if normalized != output_msgs:
                attrs["gen_ai.output.messages"] = (
                    json.dumps(normalized)
                    if isinstance(normalized, list)
                    else normalized
                )
                modified = True

        # Map Pydantic AI tool attributes to GenAI standard names
        # Pydantic AI uses: tool_arguments, tool_response
        # GenAI standard: gen_ai.tool.call.arguments, gen_ai.tool.call.result
        if "tool_arguments" in attrs and "gen_ai.tool.call.arguments" not in attrs:
            attrs["gen_ai.tool.call.arguments"] = attrs["tool_arguments"]
            modified = True

        if "tool_response" in attrs and "gen_ai.tool.call.result" not in attrs:
            attrs["gen_ai.tool.call.result"] = attrs["tool_response"]
            modified = True

        if modified:
            return _SpanWithModifiedAttributes(span, attrs)

        return span

    def _normalize_messages(self, messages: Any) -> Any:
        """
        Normalize messages from Pydantic AI format to OpenAI chat format.

        Pydantic AI: {"parts": [{"content": "...", "type": "text"}], "role": "user"}
        OpenAI:      {"content": "...", "role": "user"}
        """
        import json

        # Parse if string
        if isinstance(messages, str):
            try:
                messages = json.loads(messages)
            except json.JSONDecodeError:
                return messages

        if not isinstance(messages, list):
            return messages

        normalized: List[Dict[str, Any]] = []

        for msg in messages:
            if not isinstance(msg, dict):
                normalized.append(msg)
                continue

            # Check if this is Pydantic AI format with "parts"
            if "parts" in msg and isinstance(msg["parts"], list):
                normalized_msg = self._normalize_pydantic_message(msg)
                normalized.append(normalized_msg)
            else:
                # Already in standard format
                normalized.append(msg)

        return normalized

    def _normalize_pydantic_message(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a single Pydantic AI message to OpenAI format."""
        role = msg.get("role", "unknown")
        parts = msg.get("parts", [])

        text_parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []

        for part in parts:
            if not isinstance(part, dict):
                continue

            part_type = part.get("type", "text")

            if part_type == "text" and "content" in part:
                text_parts.append(str(part["content"]))
            elif part_type == "tool_call":
                tool_calls.append(
                    {
                        "id": part.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": part.get("name", ""),
                            "arguments": part.get("arguments", "{}"),
                        },
                    }
                )

        # Build normalized message
        normalized: Dict[str, Any] = {"role": role}

        if text_parts:
            normalized["content"] = (
                "\n".join(text_parts) if len(text_parts) > 1 else text_parts[0]
            )
        elif not tool_calls:
            normalized["content"] = ""

        if tool_calls:
            normalized["tool_calls"] = tool_calls

        # Preserve finish_reason if present (for output/assistant messages)
        if "finish_reason" in msg:
            normalized["finish_reason"] = msg["finish_reason"]

        return normalized


class _SpanWithModifiedAttributes:
    """
    Wrapper that presents a span with modified attributes.

    This allows us to transform attributes without mutating the original span.
    """

    def __init__(self, original_span: ReadableSpan, modified_attrs: Dict[str, Any]):
        self._original = original_span
        self._modified_attrs = modified_attrs

    @property
    def attributes(self) -> Dict[str, Any]:
        return self._modified_attrs

    @property
    def name(self) -> str:
        return self._original.name

    @property
    def context(self):
        return self._original.context

    @property
    def parent(self):
        return self._original.parent

    @property
    def start_time(self):
        return self._original.start_time

    @property
    def end_time(self):
        return self._original.end_time

    @property
    def status(self):
        return self._original.status

    @property
    def events(self):
        return self._original.events

    @property
    def links(self):
        return self._original.links

    @property
    def resource(self):
        return self._original.resource

    @property
    def instrumentation_scope(self):
        return self._original.instrumentation_scope
