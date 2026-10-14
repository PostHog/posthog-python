"""PostHog span processor for OpenTelemetry.

Provides a self-contained SpanProcessor that filters AI-related spans and
exports them to PostHog's OTLP endpoint. This is the recommended integration
for setups using TracerProvider.add_span_processor().
"""

import logging
import threading
from typing import Dict, Optional, Tuple

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

from ..gateway import warn_if_posthog_ai_gateway_otel_attributes
from ._request_context import (
    RequestContext,
    capture_request_context,
    with_request_context,
)
from .spans import DEFAULT_HOST, is_ai_span


log = logging.getLogger("posthog")


def _span_key(span: ReadableSpan) -> Tuple[int, int]:
    # The SDK passes on_end a ReadableSpan snapshot, not the Span object on_start saw.
    context = span.get_span_context()
    return (context.trace_id, context.span_id)


class PostHogSpanProcessor(SpanProcessor):
    """Span processor that filters AI spans and exports them to PostHog.

    Wraps a BatchSpanProcessor and OTLPSpanExporter internally, configured
    to send to PostHog's OTLP traces endpoint. Only spans identified as
    AI-related (by name or attribute prefix) are forwarded for export.

    Usage::

        from opentelemetry.sdk.trace import TracerProvider
        from posthog.ai.otel import PostHogSpanProcessor

        provider = TracerProvider()
        provider.add_span_processor(
            PostHogSpanProcessor(api_key="phc_...")
        )

        # Per request: AI spans started inside a context export with its user, session and tags.
        with posthog.new_context():
            posthog.identify_context(user_id)
            posthog.tag("plan", "pro")
            handle_request()

    Attributes already on a span win over explicit tags, which win over the
    context's distinct ID and session ID. Threads that don't copy contextvars
    carry no user.
    """

    def __init__(
        self,
        api_key: str,
        host: str = DEFAULT_HOST,
    ):
        """
        Args:
            api_key: PostHog project API key.
            host: PostHog host URL. Defaults to US cloud.
        """
        self._api_key = api_key
        self._host = host.rstrip("/")

        exporter = OTLPSpanExporter(
            endpoint=f"{self._host}/i/v0/ai/otel",
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        self._processor = BatchSpanProcessor(exporter)
        self._captured: Dict[Tuple[int, int], RequestContext] = {}
        self._captured_lock = threading.Lock()

    def on_start(self, span: Span, parent_context: Optional[Context] = None) -> None:
        """
        Remember the active PostHog context for the span.

        The span itself is left untouched, so other processors on the provider
        never see the context. The values are added to the copy exported to
        PostHog in ``on_end``.
        """
        try:
            captured = capture_request_context()
            if captured:
                with self._captured_lock:
                    self._captured[_span_key(span)] = captured
        except Exception:
            log.debug("Failed to capture PostHog context for span", exc_info=True)

    def on_end(self, span: ReadableSpan) -> None:
        """
        Export an ended span if it is AI-related.

        Args:
            span: The ended OpenTelemetry span.
        """
        with self._captured_lock:
            captured = self._captured.pop(_span_key(span), None)
        if not is_ai_span(span):
            return
        warn_if_posthog_ai_gateway_otel_attributes(span.attributes)
        if captured:
            try:
                span = with_request_context(span, captured)
            except Exception:
                log.debug("Failed to add PostHog context to span", exc_info=True)
        self._processor.on_end(span)

    def shutdown(self) -> None:
        """Shut down the underlying batch span processor."""
        self._processor.shutdown()
        with self._captured_lock:
            self._captured.clear()

    def force_flush(self, timeout_millis: Optional[int] = None) -> bool:
        """
        Flush pending spans from the underlying batch span processor.

        Args:
            timeout_millis: Optional flush timeout in milliseconds.

        Returns:
            True if the flush succeeded within the timeout, False otherwise.
        """
        if timeout_millis is not None:
            return self._processor.force_flush(timeout_millis)
        return self._processor.force_flush()
