from typing import Optional

from opentelemetry.context import Context
from opentelemetry.sdk.trace import Span, SpanProcessor

from ..tracing._context import _enrich_current_span, _enrich_span

__all__ = ["PostHogContextSpanProcessor", "enrich_current_span"]


def enrich_current_span() -> None:
    """Add request IDs to an existing recording span after request context is set.

    Explicit span attributes take precedence. Missing OpenTelemetry support and
    context lookup failures do not interrupt request handling.
    """
    _enrich_current_span()


class PostHogContextSpanProcessor(SpanProcessor):
    """Add PostHog request IDs to application-owned OpenTelemetry SDK spans.

    Add this processor to the application's existing TracerProvider before
    starting requests. It snapshots child span context at span start, without
    changing exporters, sampling or providers. Request integrations must also
    enrich an incoming span that started before request context was available.
    """

    def on_start(self, span: Span, parent_context: Optional[Context] = None) -> None:
        _enrich_span(span)
