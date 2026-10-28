"""PostHog trace exporter for OpenTelemetry.

Provides a SpanExporter that filters AI-related spans before forwarding them
to PostHog's OTLP endpoint. Use this when your setup only accepts a
SpanExporter (e.g. as an argument to BatchSpanProcessor).
"""

from typing import TYPE_CHECKING, Optional, Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

from ..gateway import warn_if_posthog_ai_gateway_otel_attributes
from ._privacy import _otel_privacy_mode_enabled, _redacted_spans
from .spans import DEFAULT_HOST, is_ai_span

if TYPE_CHECKING:
    from ...client import Client


class PostHogTraceExporter(SpanExporter):
    """Span exporter that filters AI spans and forwards them to PostHog.

    Wraps an OTLPSpanExporter configured for PostHog's OTLP endpoint. Spans
    that are not AI-related are silently dropped, returning SUCCESS immediately.
    Exporters only see finished spans, so they can't add per-request PostHog
    context; use ``PostHogSpanProcessor`` for that.

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
        *,
        privacy_mode: bool = False,
        client: Optional["Client"] = None,
    ):
        """
        Args:
            api_key: PostHog project API key.
            host: PostHog host URL. Defaults to US cloud.
            privacy_mode: Drop prompts, completions, tool arguments and results,
                and error messages from exported spans.
            client: PostHog client whose ``privacy_mode`` also applies. Without
                one, the module-level ``posthog.privacy_mode`` setting applies.
        """
        self._api_key = api_key
        self._host = host.rstrip("/")
        self._privacy_mode = privacy_mode
        self._client = client

        self._exporter = OTLPSpanExporter(
            endpoint=f"{self._host}/i/v0/ai/otel",
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """
        Export AI-related spans to PostHog and drop non-AI spans.

        Args:
            spans: Readable OpenTelemetry spans to filter and export.

        Returns:
            The OpenTelemetry export result.
        """
        ai_spans = [span for span in spans if is_ai_span(span)]
        if not ai_spans:
            return SpanExportResult.SUCCESS
        for span in ai_spans:
            warn_if_posthog_ai_gateway_otel_attributes(span.attributes)
        privacy_mode = _otel_privacy_mode_enabled(self._privacy_mode, self._client)
        ai_spans = _redacted_spans(ai_spans, privacy_mode)
        return self._exporter.export(ai_spans)

    def shutdown(self) -> None:
        """Shut down the underlying OTLP exporter."""
        self._exporter.shutdown()

    def force_flush(self, timeout_millis: Optional[int] = None) -> bool:
        """
        Flush pending spans from the underlying OTLP exporter.

        Args:
            timeout_millis: Optional flush timeout in milliseconds.

        Returns:
            True if the flush succeeded within the timeout, False otherwise.
        """
        if timeout_millis is not None:
            return self._exporter.force_flush(timeout_millis)
        return self._exporter.force_flush()
