"""PostHog trace exporter for OpenTelemetry.

Provides a SpanExporter that filters AI-related spans before forwarding them
to PostHog's OTLP endpoint. Use this when your setup only accepts a
SpanExporter (e.g. as an argument to BatchSpanProcessor).
"""

from typing import Optional, Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

from .spans import DEFAULT_HOST, is_ai_span


class PostHogTraceExporter(SpanExporter):
    """Span exporter that filters AI spans and forwards them to PostHog.

    Wraps an OTLPSpanExporter configured for PostHog's OTLP endpoint. Spans
    that are not AI-related are silently dropped, returning SUCCESS immediately.

    Usage::

        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from posthog.ai.otel import PostHogTraceExporter

        provider = TracerProvider()
        provider.add_span_processor(
            BatchSpanProcessor(
                PostHogTraceExporter(api_key="phc_...")
            )
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

        self._exporter = OTLPSpanExporter(
            endpoint=f"{self._host}/i/v0/ai/otel",
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        ai_spans = [span for span in spans if is_ai_span(span)]
        if not ai_spans:
            return SpanExportResult.SUCCESS
        return self._exporter.export(ai_spans)

    def shutdown(self) -> None:
        self._exporter.shutdown()

    def force_flush(self, timeout_millis: Optional[int] = None) -> bool:
        if timeout_millis is not None:
            return self._exporter.force_flush(timeout_millis)
        return self._exporter.force_flush()
