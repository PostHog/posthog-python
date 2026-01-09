"""
OpenTelemetry integration for PostHog AI observability.

This module provides a SpanExporter that translates OpenTelemetry spans
(particularly GenAI semantic convention spans) into PostHog AI events.
"""

from posthog.ai.otel.exporter import PostHogSpanExporter

__all__ = ["PostHogSpanExporter"]
