"""Privacy mode for OpenTelemetry spans exported to PostHog."""

from typing import TYPE_CHECKING, Optional

from opentelemetry.attributes import BoundedAttributes
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.util import BoundedList
from opentelemetry.trace.status import Status
from opentelemetry.util.types import Attributes

if TYPE_CHECKING:
    from ...client import Client

# Attributes PostHog ingestion maps into $ai_input, $ai_output_choices, $ai_input_state,
# $ai_output_state or $ai_error, plus the raw payloads it strips. Tool definitions
# ($ai_tools) are not content and stay, as in the SDK wrappers.
_CONTENT_ATTRIBUTES = frozenset(
    {
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.system_instructions",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        "anthropic.tool_result.content",
        "events",
        "pydantic_ai.all_messages",
        "final_result",
        "tool_arguments",
        "tool_response",
        "traceloop.entity.input",
        "traceloop.entity.output",
        "ai.prompt",
        "ai.prompt.messages",
        "ai.response.text",
        "ai.response.object",
        "ai.response.toolCalls",
        "ai.response.reasoning",
        "ai.toolCall.args",
        "ai.toolCall.result",
        "ai.value",
        "ai.values",
        "input.value",
        "output.value",
        "llm.function_call",
        "llm.invocation_parameters",
        "exception.message",
        "exception.stacktrace",
        "error.message",
    }
)

# Matched as the attribute itself or as a dotted prefix, e.g. `gen_ai.prompt.0.content`.
_CONTENT_ATTRIBUTE_ROOTS = (
    "gen_ai.prompt",
    "gen_ai.completion",
    "llm.prompts",
    "llm.input_messages",
    "llm.output_messages",
    "llm.choices",
    "llm.prompt_template.template",
    "llm.prompt_template.variables",
)

_EXCEPTION_EVENT_NAME = "exception"


def _is_embedding_text(key: str) -> bool:
    """OpenInference stores embedding inputs as `embedding.embeddings.<i>.embedding.text`."""
    return key.startswith("embedding.embeddings.") and key.endswith(".embedding.text")


def _is_content_attribute(key: str) -> bool:
    """Whether a span or event attribute can carry prompts, completions, tool data or error text."""
    if key in _CONTENT_ATTRIBUTES or _is_embedding_text(key):
        return True
    return any(
        key == root or key.startswith(f"{root}.") for root in _CONTENT_ATTRIBUTE_ROOTS
    )


def _otel_privacy_mode_enabled(privacy_mode: bool, client: Optional["Client"]) -> bool:
    """Explicit flag, else the given client's `privacy_mode`, else the module-level configuration."""
    if privacy_mode:
        return True
    if client is not None:
        return getattr(client, "privacy_mode", False) is True
    # Imported here so later changes to the module-level settings still apply.
    from ... import default_client, privacy_mode as global_privacy_mode

    return bool(global_privacy_mode) or (
        getattr(default_client, "privacy_mode", False) is True
    )


def _without_content(attributes: Attributes) -> BoundedAttributes:
    return BoundedAttributes(
        attributes={
            key: value
            for key, value in (attributes or {}).items()
            if not _is_content_attribute(key)
        }
    )


def _redact_span(span: ReadableSpan) -> ReadableSpan:
    """
    Return a copy of ``span`` without content attributes, non-exception events,
    exception messages and stack traces, or the status description.
    """
    # Older OTLP encoders read `.dropped` from these collections, so plain dicts and
    # lists break export.
    events = BoundedList.from_seq(
        None,
        [
            Event(
                name=event.name,
                attributes=_without_content(event.attributes),
                timestamp=event.timestamp,
            )
            # Other span events (older GenAI semconv messages, framework logs) carry
            # content and PostHog ingestion only reads exception events.
            for event in span.events
            if event.name == _EXCEPTION_EVENT_NAME
        ],
    )
    return ReadableSpan(
        name=span.name,
        context=span.get_span_context(),
        parent=span.parent,
        resource=span.resource,
        attributes=_without_content(span.attributes),
        events=events,
        links=BoundedList.from_seq(None, span.links),
        kind=span.kind,
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )
