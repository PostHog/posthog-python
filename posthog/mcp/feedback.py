"""The ``send_feedback`` virtual tool: an opt-in tool advertised to agents so they
can send feedback about the server to its developers — a missing capability (the
priority category), a tool that failed or confused them, or praise. Calling it
emits ``$mcp_feedback`` (not ``$mcp_tool_call``)."""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, Optional, Union

from ._internal import _maybe_await
from ._sanitization import sanitize_free_text, sanitize_free_text_value
from .constants import PostHogMCPAnalyticsProperty
from .logger import log
from .types import CollectFeedbackOptions, FeedbackReport, JsonRecord

__all__ = ["SEND_FEEDBACK_TOOL_NAME", "send_feedback_result"]

SEND_FEEDBACK_TOOL_NAME = "send_feedback"

_FEEDBACK_TYPES = ("missing_capability", "issue", "praise", "other")
_SENTIMENTS = ("positive", "neutral", "negative", "mixed")

# Free-text fields are agent-narrated, like `$mcp_intent`; bound them the same way.
_MAX_FEEDBACK_TEXT_LENGTH = 2048
_MAX_FEEDBACK_TOOL_NAME_LENGTH = 256
_TRUNCATION_SUFFIX = "..."

_DEFAULT_FEEDBACK_DESCRIPTION = (
    "Send feedback about this server to its developers. Most important: report a missing "
    "capability whenever no available tool fits your task, even if you can work around it "
    '(feedback_type "missing_capability"). Also welcome: a tool that failed or confused you, '
    "an unhelpful error, or something that worked well. This records the feedback; it does "
    "not add or change tools. Do not include user PII or sensitive content."
)

_SEND_FEEDBACK_RESULT_TEXT = (
    "Your feedback was recorded for the server's developers. No additional tools are "
    "available - continue with the tools already listed."
)

_CORE_FEEDBACK_SCHEMA_PROPERTIES: Dict[str, Dict[str, Any]] = {
    "feedback_type": {
        "type": "string",
        "enum": list(_FEEDBACK_TYPES),
        "description": (
            "What kind of feedback this is. Use 'missing_capability' when the tool you "
            "needed does not exist in the tool list - nothing failed, the capability is "
            "absent (this is the most valuable report; send it even if you found a "
            "workaround). Use 'issue' when an existing tool behaved badly: it failed, "
            "returned a confusing error, or its description or schema misled you. Use "
            "'praise' when something worked notably well. Use 'other' for anything else."
        ),
    },
    "summary": {
        "type": "string",
        "description": (
            "One self-contained sentence. For 'missing_capability': the capability you "
            "needed, e.g. 'No tool to delete multiple cohorts in one call.' For 'issue': "
            "the tool and the problem, e.g. 'query-trends rejects relative date ranges "
            "with an unclear error.'"
        ),
    },
    "details": {
        "type": "string",
        "description": (
            "Optional longer context: what you tried, exact parameter values, the error "
            "text you saw, and any workaround you used. Omit when the summary says it all."
        ),
    },
    "friction_points": {
        "type": "string",
        "description": (
            "Optional: the specific moments that slowed you down, as short bullet-like "
            "sentences, quoting exact tool names, parameters, or error text."
        ),
    },
    "suggested_improvement": {
        "type": "string",
        "description": (
            "Optional: the concrete change that would have helped, e.g. the tool to add, "
            "the description to reword, or the error message to improve."
        ),
    },
    "tool_name": {
        "type": "string",
        "description": (
            "Optional: the existing tool this feedback is about (for 'issue' or 'praise'). "
            "Leave empty for 'missing_capability' - the point is that no tool fits."
        ),
    },
    "sentiment": {
        "type": "string",
        "enum": list(_SENTIMENTS),
        "description": "Optional: how the experience felt overall.",
    },
    "task_completed": {
        "type": "boolean",
        "description": "Optional: whether you still completed the user's task despite the problem.",
    },
}

# Extra-property names a host may not declare: the core fields themselves, the
# names whose `$mcp_feedback_<key>` property would collide with a core property
# (`type` -> `$mcp_feedback_type`, `tool` -> `$mcp_feedback_tool`), and the
# SDK-injected analytics arguments — the report is parsed from the raw arguments
# before those are stripped, so an extra by the same name would capture an
# SDK-owned value.
_RESERVED_EXTRA_PROPERTY_KEYS = frozenset(_CORE_FEEDBACK_SCHEMA_PROPERTIES) | {
    "type",
    "tool",
    "context",
    "conversation_id",
    "llm_model",
}


def resolve_collect_feedback_options(
    config: Union[bool, CollectFeedbackOptions, None],
) -> Optional[CollectFeedbackOptions]:
    """``collect_feedback`` normalized to its object form; ``None`` when the
    feature is off."""
    if not config:
        return None
    return CollectFeedbackOptions() if config is True else config


def resolve_send_feedback_tool_name(options: Optional[CollectFeedbackOptions]) -> str:
    """The configured name of the virtual tool, falling back to the default.
    Resolve through here everywhere (inject + detect) so a custom name can't drift."""
    name = options.tool_name if options is not None else None
    return name or SEND_FEEDBACK_TOOL_NAME


def get_feedback_tool_descriptor(
    options: Optional[CollectFeedbackOptions] = None,
) -> Dict[str, Any]:
    """The advertised descriptor: the core feedback schema plus the host's declared
    ``extra_properties`` (plain dict; adapters build the framework's Tool object
    from it). Raises ``ValueError`` on a config error (a reserved extra key, or an
    ``extra_required`` entry that was never declared) so a bad setup fails at
    configuration time instead of silently corrupting the advertised schema."""
    options = options or CollectFeedbackOptions()
    extra_properties = options.extra_properties or {}
    extra_required = options.extra_required or []

    for key in extra_properties:
        if key in _RESERVED_EXTRA_PROPERTY_KEYS:
            raise ValueError(
                f'collect_feedback extra_properties key "{key}" collides with a core or '
                "SDK-reserved send_feedback field. Rename it."
            )
    for key in extra_required:
        if key not in extra_properties:
            raise ValueError(
                f'collect_feedback extra_required key "{key}" is not declared in extra_properties.'
            )

    # Deep copies: the host may reuse or mutate the fragments it handed us, and
    # schema injection mutates the advertised descriptor.
    properties: Dict[str, Any] = copy.deepcopy(_CORE_FEEDBACK_SCHEMA_PROPERTIES)
    if extra_properties:
        properties.update(copy.deepcopy(extra_properties))

    return {
        "name": resolve_send_feedback_tool_name(options),
        "description": options.description or _DEFAULT_FEEDBACK_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": ["feedback_type", "summary", *extra_required],
        },
        "annotations": {
            "title": "Send feedback",
            "readOnlyHint": True,
            # Interacts with an external entity: the report lands in analytics.
            "openWorldHint": True,
            # Only records the feedback, so repeat calls are harmless — and
            # advertising it as idempotent makes agents more willing to call it
            # proactively.
            "idempotentHint": True,
            "destructiveHint": False,
        },
    }


def _read_string(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None


def _matches_extra_schema(value: Any, schema: Dict[str, Any]) -> bool:
    """True when the value conforms to the declared fragment's ``type`` and
    ``enum`` — the same advisory-schema enforcement the core fields get, so
    ``extras`` only ever holds schema-conforming values and a misbehaving agent
    shows up as absence rather than as an unexpected shape in the host's handler."""
    if isinstance(value, list):
        actual = "array"
    elif value is None:
        actual = "null"
    elif isinstance(value, bool):
        actual = "boolean"
    elif isinstance(value, (int, float)):
        actual = "number"
    elif isinstance(value, str):
        actual = "string"
    else:
        actual = "object"
    declared = schema.get("type")
    if declared != actual and not (declared == "integer" and actual == "number"):
        return False
    enum = schema.get("enum")
    return not isinstance(enum, list) or value in enum


def parse_feedback_report(
    args: Optional[Dict[str, Any]],
    options: Optional[CollectFeedbackOptions] = None,
) -> FeedbackReport:
    """Parse the raw ``send_feedback`` arguments into a typed report. Never raises:
    an invalid ``feedback_type`` falls back to ``other``, missing fields stay
    ``None``, and only **declared** extras whose values match their declared
    ``type``/``enum`` are lifted into ``extras`` — mismatches and anything the
    agent invented reach the handler via ``raw`` only and are never captured."""
    raw = args or {}
    declared = (options.extra_properties if options is not None else None) or {}
    extras = {
        key: raw[key]
        for key, schema in declared.items()
        if raw.get(key) is not None and _matches_extra_schema(raw[key], schema)
    }
    feedback_type = raw.get("feedback_type")
    sentiment = raw.get("sentiment")
    task_completed = raw.get("task_completed")
    return FeedbackReport(
        feedback_type=feedback_type if feedback_type in _FEEDBACK_TYPES else "other",
        summary=_read_string(raw.get("summary")) or "",
        sentiment=sentiment if sentiment in _SENTIMENTS else None,
        friction_points=_read_string(raw.get("friction_points")),
        suggested_improvement=_read_string(raw.get("suggested_improvement")),
        details=_read_string(raw.get("details")),
        tool_name=_read_string(raw.get("tool_name")),
        task_completed=task_completed if isinstance(task_completed, bool) else None,
        extras=extras,
        raw=dict(raw),
    )


def build_feedback_intent(report: FeedbackReport) -> str:
    """The report's free text, used as the event's ``$mcp_intent``."""
    return "\n\n".join(part for part in (report.summary, report.details) if part)


def _truncate_feedback_text(value: str, max_length: int) -> str:
    return value[:max_length] + _TRUNCATION_SUFFIX if len(value) > max_length else value


def _capture_free_text(value: str) -> str:
    """Agent-narrated free text can contain a secret the LLM read aloud or personal
    data it narrated, so it gets exactly the ``$mcp_intent`` pass
    (``sanitize_free_text``: credentials -> structured PII -> URLs — the order is
    load-bearing, the URL rewrite would percent-encode the ``@`` the email pattern
    anchors on), then a length bound. The event pipeline does not process
    ``event["properties"]``, so this happens here."""
    return _truncate_feedback_text(sanitize_free_text(value), _MAX_FEEDBACK_TEXT_LENGTH)


def _capture_extra_value(value: Any) -> Any:
    """A declared extra is agent-supplied like the core free-text fields, so its
    string leaves get the same free-text pass (with the key-based redaction
    ``sanitize_free_text_value`` keeps for nested objects), then non-scalars are
    JSON-stringified and everything is bounded. Unserializable values are dropped."""
    sanitized = sanitize_free_text_value(value)
    if isinstance(sanitized, str):
        return _truncate_feedback_text(sanitized, _MAX_FEEDBACK_TEXT_LENGTH)
    if sanitized is None or isinstance(sanitized, (bool, int, float)):
        return sanitized
    try:
        return _truncate_feedback_text(json.dumps(sanitized), _MAX_FEEDBACK_TEXT_LENGTH)
    except Exception:  # noqa: BLE001 - capture must never raise into the tool path
        return None


def build_feedback_event_properties(report: FeedbackReport) -> JsonRecord:
    """The ``$mcp_feedback_*`` event properties for one report, declared extras
    included."""
    properties: JsonRecord = {
        PostHogMCPAnalyticsProperty.FEEDBACK_TYPE: report.feedback_type
    }
    if report.summary:
        properties[PostHogMCPAnalyticsProperty.FEEDBACK_SUMMARY] = _capture_free_text(
            report.summary
        )
    if report.sentiment:
        properties[PostHogMCPAnalyticsProperty.FEEDBACK_SENTIMENT] = report.sentiment
    if report.friction_points:
        properties[PostHogMCPAnalyticsProperty.FEEDBACK_FRICTION_POINTS] = (
            _capture_free_text(report.friction_points)
        )
    if report.suggested_improvement:
        properties[PostHogMCPAnalyticsProperty.FEEDBACK_SUGGESTED_IMPROVEMENT] = (
            _capture_free_text(report.suggested_improvement)
        )
    if report.details:
        properties[PostHogMCPAnalyticsProperty.FEEDBACK_DETAILS] = _capture_free_text(
            report.details
        )
    if report.tool_name:
        # Nominally an identifier, but the schema can't stop an agent from
        # writing prose into it — so it gets the same free-text pass as the
        # other fields.
        properties[PostHogMCPAnalyticsProperty.FEEDBACK_TOOL] = _truncate_feedback_text(
            sanitize_free_text(report.tool_name),
            _MAX_FEEDBACK_TOOL_NAME_LENGTH,
        )
    if report.task_completed is not None:
        properties[PostHogMCPAnalyticsProperty.FEEDBACK_TASK_COMPLETED] = (
            report.task_completed
        )
    for key, value in report.extras.items():
        captured = _capture_extra_value(value)
        if captured is not None:
            properties[f"$mcp_feedback_{key}"] = captured
    return properties


def send_feedback_result() -> Dict[str, Any]:
    """The canned acknowledgement returned to the agent after it calls
    ``send_feedback``. Reply with this from a custom dispatcher; the
    ``instrument()`` path returns it automatically, or the string your
    ``on_feedback`` handler returned instead."""
    return {"content": [{"type": "text", "text": _SEND_FEEDBACK_RESULT_TEXT}]}


def send_feedback_result_text() -> str:
    return _SEND_FEEDBACK_RESULT_TEXT


async def handle_feedback(
    report: FeedbackReport, options: Optional[CollectFeedbackOptions] = None
) -> str:
    """Run the host's ``on_feedback`` handler (when configured) and return the
    reply text. A returned non-blank string replaces the default acknowledgement;
    a raised handler is logged and falls back to it — feedback capture must never
    break the agent's turn."""
    # Only the type: the summary is agent-narrated free text (possible PII,
    # newlines for log forging, unbounded length) and does not belong in host logs.
    log(f"Agent feedback reported ({report.feedback_type})")
    if options is not None and options.on_feedback is not None:
        try:
            reply = await _maybe_await(options.on_feedback(report))
            if isinstance(reply, str) and reply.strip():
                return reply
        except Exception as error:  # noqa: BLE001 - never break the agent's turn
            log(
                "Warning: on_feedback handler threw; returning the default "
                f"acknowledgement - {error}"
            )
    return _SEND_FEEDBACK_RESULT_TEXT
