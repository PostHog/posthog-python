"""PostHog OpenTelemetry integration for AI tracing.

Provides components to route AI-related OpenTelemetry spans to PostHog's
OTLP endpoint. Only spans matching known AI semantic convention prefixes
(gen_ai, llm, ai, traceloop) are forwarded; all other spans are silently
dropped.

Two integration patterns are supported:

1. **PostHogSpanProcessor** (recommended) - Self-contained processor that
   handles batching and export internally::

       provider = TracerProvider()
       provider.add_span_processor(
           PostHogSpanProcessor(api_key="phc_...")
       )

2. **PostHogTraceExporter** - Exporter for use with your own
   BatchSpanProcessor or frameworks that only accept a SpanExporter::

       provider = TracerProvider()
       provider.add_span_processor(
           BatchSpanProcessor(
               PostHogTraceExporter(api_key="phc_...")
           )
       )
"""

from posthog.ai.otel.exporter import PostHogTraceExporter
from posthog.ai.otel.processor import PostHogSpanProcessor
from posthog.ai.otel.spans import is_ai_span

__all__ = ["PostHogSpanProcessor", "PostHogTraceExporter", "is_ai_span"]
