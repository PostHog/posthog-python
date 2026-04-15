import unittest
from unittest.mock import patch

from opentelemetry.sdk.trace.export import SpanExportResult

from posthog.ai.otel.exporter import PostHogTraceExporter
from posthog.test.ai.otel.conftest import make_span


class TestPostHogTraceExporter(unittest.TestCase):
    @patch("posthog.ai.otel.exporter.OTLPSpanExporter")
    def test_configures_exporter_with_correct_endpoint(self, mock_otlp_cls):
        PostHogTraceExporter(api_key="phc_test123")
        mock_otlp_cls.assert_called_once_with(
            endpoint="https://us.i.posthog.com/i/v0/ai/otel",
            headers={"Authorization": "Bearer phc_test123"},
        )

    @patch("posthog.ai.otel.exporter.OTLPSpanExporter")
    def test_configures_custom_host(self, mock_otlp_cls):
        PostHogTraceExporter(api_key="phc_test", host="https://eu.i.posthog.com")
        mock_otlp_cls.assert_called_once_with(
            endpoint="https://eu.i.posthog.com/i/v0/ai/otel",
            headers={"Authorization": "Bearer phc_test"},
        )

    @patch("posthog.ai.otel.exporter.OTLPSpanExporter")
    def test_exports_ai_spans(self, mock_otlp_cls):
        exporter = PostHogTraceExporter(api_key="phc_test")
        inner = mock_otlp_cls.return_value
        inner.export.return_value = SpanExportResult.SUCCESS

        spans = [make_span("gen_ai.chat"), make_span("llm.call")]
        result = exporter.export(spans)

        self.assertEqual(result, SpanExportResult.SUCCESS)
        inner.export.assert_called_once_with(spans)

    @patch("posthog.ai.otel.exporter.OTLPSpanExporter")
    def test_filters_out_non_ai_spans(self, mock_otlp_cls):
        exporter = PostHogTraceExporter(api_key="phc_test")
        inner = mock_otlp_cls.return_value
        inner.export.return_value = SpanExportResult.SUCCESS

        ai_span = make_span("gen_ai.chat")
        http_span = make_span("http.request")
        result = exporter.export([ai_span, http_span])

        self.assertEqual(result, SpanExportResult.SUCCESS)
        inner.export.assert_called_once_with([ai_span])

    @patch("posthog.ai.otel.exporter.OTLPSpanExporter")
    def test_returns_success_when_no_ai_spans(self, mock_otlp_cls):
        exporter = PostHogTraceExporter(api_key="phc_test")
        inner = mock_otlp_cls.return_value

        result = exporter.export([make_span("http.request"), make_span("db.query")])

        self.assertEqual(result, SpanExportResult.SUCCESS)
        inner.export.assert_not_called()

    @patch("posthog.ai.otel.exporter.OTLPSpanExporter")
    def test_returns_success_for_empty_batch(self, mock_otlp_cls):
        exporter = PostHogTraceExporter(api_key="phc_test")
        inner = mock_otlp_cls.return_value

        result = exporter.export([])

        self.assertEqual(result, SpanExportResult.SUCCESS)
        inner.export.assert_not_called()

    @patch("posthog.ai.otel.exporter.OTLPSpanExporter")
    def test_exports_spans_with_ai_attributes(self, mock_otlp_cls):
        exporter = PostHogTraceExporter(api_key="phc_test")
        inner = mock_otlp_cls.return_value
        inner.export.return_value = SpanExportResult.SUCCESS

        span = make_span("http.request", {"gen_ai.system": "openai"})
        result = exporter.export([span])

        self.assertEqual(result, SpanExportResult.SUCCESS)
        inner.export.assert_called_once_with([span])

    @patch("posthog.ai.otel.exporter.OTLPSpanExporter")
    def test_shutdown_delegates(self, mock_otlp_cls):
        exporter = PostHogTraceExporter(api_key="phc_test")
        inner = mock_otlp_cls.return_value

        exporter.shutdown()
        inner.shutdown.assert_called_once()

    @patch("posthog.ai.otel.exporter.OTLPSpanExporter")
    def test_force_flush_delegates(self, mock_otlp_cls):
        exporter = PostHogTraceExporter(api_key="phc_test")
        inner = mock_otlp_cls.return_value

        exporter.force_flush(timeout_millis=5000)
        inner.force_flush.assert_called_once_with(5000)
