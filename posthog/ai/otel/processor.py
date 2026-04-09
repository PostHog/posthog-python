"""PostHog span processor for OpenTelemetry.

Provides a self-contained SpanProcessor that filters AI-related spans and
exports them to PostHog's OTLP endpoint. This is the recommended integration
for setups using TracerProvider.add_span_processor().
"""

from typing import Optional

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

from .spans import DEFAULT_HOST, is_ai_span


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

    def on_start(self, span: Span, parent_context: Optional[Context] = None) -> None:
        pass

    def on_end(self, span: ReadableSpan) -> None:
        if not is_ai_span(span):
            return
        self._processor.on_end(span)

    def shutdown(self) -> None:
        self._processor.shutdown()

    def force_flush(self, timeout_millis: Optional[int] = None) -> bool:
        if timeout_millis is not None:
            return self._processor.force_flush(timeout_millis)
        return self._processor.force_flush()
