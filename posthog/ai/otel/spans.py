"""Shared AI span filtering logic and constants for OpenTelemetry integration."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan

DEFAULT_HOST = "https://us.i.posthog.com"

AI_SPAN_PREFIXES = ("gen_ai.", "llm.", "ai.", "traceloop.")


def is_ai_span(span: "ReadableSpan") -> bool:
    """Check if a span is AI-related by examining its name and attribute keys.

    Matches spans whose name or any attribute key starts with one of the
    known AI semantic convention prefixes (gen_ai, llm, ai, traceloop).
    """
    name = span.name
    if any(name.startswith(prefix) for prefix in AI_SPAN_PREFIXES):
        return True

    attributes = span.attributes or {}
    for key in attributes:
        if any(key.startswith(prefix) for prefix in AI_SPAN_PREFIXES):
            return True

    return False
