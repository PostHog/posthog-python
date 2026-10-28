"""Privacy mode for OpenTelemetry spans exported to PostHog."""

import json
import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

from opentelemetry.attributes import BoundedAttributes
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.util import BoundedList
from opentelemetry.trace.status import Status
from opentelemetry.util.types import Attributes, AttributeValue

from ..utils import _redact_mcp_tool, _redact_mcp_tool_credentials

if TYPE_CHECKING:
    from ...client import Client

log = logging.getLogger("posthog")

# Attributes that carry prompts, outputs, tool calls, retrieved documents or error text.
# Ingestion keeps unmapped attributes as event properties, so this also covers attributes
# it does not map. Tool definitions ($ai_tools) are not content and stay, as in the SDK
# wrappers.
_CONTENT_ATTRIBUTES = frozenset(
    {
        # Vercel AI SDK
        "ai.prompt",
        "ai.prompt.messages",
        "ai.request.text",
        "ai.documents",
        "ai.response.text",
        "ai.response.object",
        "ai.response.toolCalls",
        "ai.response.reasoning",
        "ai.response.reasoningDetails",
        "ai.response.files",
        "ai.toolCall.args",
        "ai.toolCall.result",
        "ai.decision.state",
        "ai.decision.questions",
        "ai.decision.answers",
        "ai.value",
        "ai.values",
        "ai.embedding",
        "ai.embeddings",
        # OpenTelemetry GenAI semantic conventions
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.system_instructions",
        "gen_ai.prompt",
        "gen_ai.completion",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        "gen_ai.retrieval.documents",
        "gen_ai.retrieval.query.text",
        "gen_ai.memory.query.text",
        "gen_ai.memory.records",
        "gen_ai.evaluation.explanation",
        # Traceloop / OpenLLMetry
        "traceloop.entity.input",
        "traceloop.entity.output",
        "traceloop.prompt.template",
        "gen_ai.task.input",
        "gen_ai.task.output",
        "gen_ai.guardrail.input",
        "gen_ai.guardrail.output",
        "gen_ai.guardrail.error.message",
        "gen_ai.response.revised_prompt",
        "mcp.response.value",
        "db.marqo.search.query",
        # Anthropic
        "anthropic.tool_result.content",
        # Pydantic AI / Logfire
        "pydantic_ai.all_messages",
        "request_data",
        "response_data",
        "model_request_parameters",
        "final_result",
        "tool_arguments",
        "tool_response",
        "inputs",
        "prompt",
        "events",
        # OpenInference
        "input.value",
        "output.value",
        "llm.function_call",
        "llm.prompt_template.template",
        "llm.prompt_template.variables",
        # Holds request parameters such as Responses `instructions`.
        "llm.invocation_parameters",
        "reranker.query",
        # Langfuse
        "langfuse.trace.input",
        "langfuse.trace.output",
        "langfuse.observation.input",
        "langfuse.observation.output",
        "langfuse.observation.status_message",
        "langfuse.experiment.item.expected_output",
        # Errors
        "exception.message",
        "exception.stacktrace",
        "error.message",
    }
)

_CONTENT_ATTRIBUTE_PREFIXES = (
    "gen_ai.prompt.",
    "gen_ai.completion.",
    "llm.prompts.",
    "llm.completions.",
    "llm.input_messages.",
    "llm.output_messages.",
    "llm.choices.",
    "traceloop.prompt.template_variables.",
)

_CONTENT_ATTRIBUTE_PATTERN = re.compile(
    "|".join(
        (
            r"embedding\.embeddings\.\d+\.embedding\.(text|vector)",
            r"retrieval\.documents\.\d+\.document\.(content|metadata)",
            r"reranker\.(input|output)_documents\.\d+\.document\.content",
            r"(input|output)\.images\.\d+\.image\.url",
            r"db\.chroma\.(query|get|delete)\.where_document",
            r"mastra\.[a-z_]+\.(input|output)",
        )
    )
)

# GenAI semantic convention prompt metadata, not content, under the `gen_ai.prompt.` prefix.
_NON_CONTENT_ATTRIBUTES = frozenset({"gen_ai.prompt.name", "gen_ai.prompt.version"})

_PROVIDER_METADATA_ATTRIBUTE = "ai.response.providerMetadata"

# Tool definitions stay under privacy, but instrumentations serialize remote MCP tools
# with their credentials into them.
_TOOL_DEFINITION_ATTRIBUTES = frozenset({"gen_ai.tool.definitions", "ai.prompt.tools"})
_TOOL_DEFINITION_PATTERN = re.compile(r"llm\.tools\.\d+\.tool\.json_schema")

_EXCEPTION_EVENT_NAME = "exception"


def _is_content_attribute(key: str) -> bool:
    """Whether a span or event attribute can carry prompts, completions, tool data or error text."""
    if key in _NON_CONTENT_ATTRIBUTES:
        return False
    return (
        key in _CONTENT_ATTRIBUTES
        or key.startswith(_CONTENT_ATTRIBUTE_PREFIXES)
        or f"{key}." in _CONTENT_ATTRIBUTE_PREFIXES
        or _CONTENT_ATTRIBUTE_PATTERN.fullmatch(key) is not None
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


def _gateway_cost_only(value: Any) -> Optional[str]:
    """
    Provider metadata can carry content, such as Google grounding search queries.
    Ingestion only reads the Vercel AI Gateway cost from it.
    """
    if not isinstance(value, str):
        return None
    try:
        metadata = json.loads(value)
    except ValueError:
        return None
    gateway = metadata.get("gateway") if isinstance(metadata, dict) else None
    cost = gateway.get("cost") if isinstance(gateway, dict) else None
    if isinstance(cost, bool) or not isinstance(cost, (int, float, str)):
        return None
    return json.dumps({"gateway": {"cost": cost}}, separators=(",", ":"))


def _redact_tool_definition_json(value: str, drop_unparseable: bool) -> Optional[str]:
    try:
        parsed = json.loads(value)
    except ValueError:
        parsed = None
    if isinstance(parsed, list):
        redacted = _redact_mcp_tool_credentials(parsed)
    elif isinstance(parsed, dict):
        redacted = _redact_mcp_tool(parsed)
    else:
        return None if drop_unparseable else value
    return value if redacted is parsed else json.dumps(redacted)


def _redact_tool_definitions(
    value: Any, drop_unparseable: bool
) -> Optional[AttributeValue]:
    """
    Mask remote MCP credentials in a tool definition attribute. Under privacy mode a
    value that cannot be parsed is dropped, because it could still hold credentials.
    """
    if isinstance(value, str):
        return _redact_tool_definition_json(value, drop_unparseable)
    if not isinstance(value, (list, tuple)):
        return None if drop_unparseable else value
    redacted: List[Any] = []
    for item in value:
        definition = (
            _redact_tool_definition_json(item, drop_unparseable)
            if isinstance(item, str)
            else None
            if drop_unparseable
            else item
        )
        if definition is not None:
            redacted.append(definition)
    if len(redacted) == len(value) and all(a is b for a, b in zip(redacted, value)):
        return value
    return redacted


def _is_tool_definition_attribute(key: str) -> bool:
    return (
        key in _TOOL_DEFINITION_ATTRIBUTES
        or _TOOL_DEFINITION_PATTERN.fullmatch(key) is not None
    )


def _without_content(attributes: Attributes) -> BoundedAttributes:
    kept: Dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if _is_content_attribute(key):
            continue
        if _is_tool_definition_attribute(key):
            definitions = _redact_tool_definitions(value, drop_unparseable=True)
            if definitions is not None:
                kept[key] = definitions
            continue
        if key == _PROVIDER_METADATA_ATTRIBUTE:
            metadata = _gateway_cost_only(value)
            if metadata is not None:
                kept[key] = metadata
            continue
        kept[key] = value
    return BoundedAttributes(attributes=kept)


def _without_content_span(span: ReadableSpan) -> ReadableSpan:
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


def _without_mcp_credentials_span(span: ReadableSpan) -> ReadableSpan:
    """Return ``span`` with remote MCP credentials masked in its tool definitions."""
    attributes = dict(span.attributes or {})
    redacted: Dict[str, Any] = {}
    for key, value in attributes.items():
        if not _is_tool_definition_attribute(key):
            redacted[key] = value
            continue
        definitions = _redact_tool_definitions(value, drop_unparseable=False)
        if definitions is not None:
            redacted[key] = definitions
    if redacted.keys() == attributes.keys() and all(
        redacted[key] is attributes[key] for key in attributes
    ):
        return span
    return ReadableSpan(
        name=span.name,
        context=span.get_span_context(),
        parent=span.parent,
        resource=span.resource,
        attributes=BoundedAttributes(attributes=redacted),
        events=BoundedList.from_seq(None, span.events),
        links=BoundedList.from_seq(None, span.links),
        kind=span.kind,
        status=span.status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


def _redact_span(span: ReadableSpan, privacy_mode: bool) -> ReadableSpan:
    """
    Mask remote MCP credentials in tool definitions on every export, as for
    ``$ai_tools``. Privacy mode also removes all content.
    """
    if privacy_mode:
        return _without_content_span(span)
    return _without_mcp_credentials_span(span)


def _redacted_spans(
    spans: Sequence[ReadableSpan], privacy_mode: bool
) -> List[ReadableSpan]:
    """Redact each span, dropping only a span whose redaction fails rather than leaking it."""
    redacted = []
    for span in spans:
        try:
            redacted.append(_redact_span(span, privacy_mode))
        except Exception:
            log.debug("Failed to redact span, dropping it", exc_info=True)
    return redacted
