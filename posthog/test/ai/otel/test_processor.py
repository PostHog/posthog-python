import unittest
from unittest.mock import MagicMock, patch

from posthog.ai.otel.processor import PostHogSpanProcessor
from posthog.test.ai.otel.conftest import make_span


class TestPostHogSpanProcessor(unittest.TestCase):
    @patch("posthog.ai.otel.processor.OTLPSpanExporter")
    @patch("posthog.ai.otel.processor.BatchSpanProcessor")
    def test_configures_exporter_with_correct_endpoint(
        self, mock_batch_cls, mock_otlp_cls
    ):
        PostHogSpanProcessor(api_key="phc_test123")
        mock_otlp_cls.assert_called_once_with(
            endpoint="https://us.i.posthog.com/i/v0/ai/otel",
            headers={"Authorization": "Bearer phc_test123"},
        )
        mock_batch_cls.assert_called_once_with(mock_otlp_cls.return_value)

    @patch("posthog.ai.otel.processor.OTLPSpanExporter")
    @patch("posthog.ai.otel.processor.BatchSpanProcessor")
    def test_configures_custom_host(self, mock_batch_cls, mock_otlp_cls):
        PostHogSpanProcessor(api_key="phc_test", host="https://eu.i.posthog.com")
        mock_otlp_cls.assert_called_once_with(
            endpoint="https://eu.i.posthog.com/i/v0/ai/otel",
            headers={"Authorization": "Bearer phc_test"},
        )

    @patch("posthog.ai.otel.processor.OTLPSpanExporter")
    @patch("posthog.ai.otel.processor.BatchSpanProcessor")
    def test_strips_trailing_slash_from_host(self, mock_batch_cls, mock_otlp_cls):
        PostHogSpanProcessor(api_key="phc_test", host="https://us.i.posthog.com/")
        mock_otlp_cls.assert_called_once_with(
            endpoint="https://us.i.posthog.com/i/v0/ai/otel",
            headers={"Authorization": "Bearer phc_test"},
        )

    @patch("posthog.ai.otel.processor.OTLPSpanExporter")
    @patch("posthog.ai.otel.processor.BatchSpanProcessor")
    def test_forwards_ai_spans(self, mock_batch_cls, mock_otlp_cls):
        processor = PostHogSpanProcessor(api_key="phc_test")
        inner = mock_batch_cls.return_value

        ai_span = make_span("gen_ai.chat")
        processor.on_end(ai_span)
        inner.on_end.assert_called_once_with(ai_span)

    @patch("posthog.ai.otel.processor.OTLPSpanExporter")
    @patch("posthog.ai.otel.processor.BatchSpanProcessor")
    def test_drops_non_ai_spans(self, mock_batch_cls, mock_otlp_cls):
        processor = PostHogSpanProcessor(api_key="phc_test")
        inner = mock_batch_cls.return_value

        processor.on_end(make_span("http.request"))
        inner.on_end.assert_not_called()

    @patch("posthog.ai.otel.processor.OTLPSpanExporter")
    @patch("posthog.ai.otel.processor.BatchSpanProcessor")
    def test_forwards_span_with_ai_attributes(self, mock_batch_cls, mock_otlp_cls):
        processor = PostHogSpanProcessor(api_key="phc_test")
        inner = mock_batch_cls.return_value

        span = make_span("http.request", {"gen_ai.system": "openai"})
        processor.on_end(span)
        inner.on_end.assert_called_once_with(span)

    @patch("posthog.ai.otel.processor.OTLPSpanExporter")
    @patch("posthog.ai.otel.processor.BatchSpanProcessor")
    def test_on_start_is_noop(self, mock_batch_cls, mock_otlp_cls):
        processor = PostHogSpanProcessor(api_key="phc_test")
        inner = mock_batch_cls.return_value

        processor.on_start(MagicMock())
        inner.on_start.assert_not_called()

    @patch("posthog.ai.otel.processor.OTLPSpanExporter")
    @patch("posthog.ai.otel.processor.BatchSpanProcessor")
    def test_shutdown_delegates(self, mock_batch_cls, mock_otlp_cls):
        processor = PostHogSpanProcessor(api_key="phc_test")
        inner = mock_batch_cls.return_value

        processor.shutdown()
        inner.shutdown.assert_called_once()

    @patch("posthog.ai.otel.processor.OTLPSpanExporter")
    @patch("posthog.ai.otel.processor.BatchSpanProcessor")
    def test_force_flush_delegates(self, mock_batch_cls, mock_otlp_cls):
        processor = PostHogSpanProcessor(api_key="phc_test")
        inner = mock_batch_cls.return_value

        processor.force_flush(timeout_millis=5000)
        inner.force_flush.assert_called_once_with(5000)

    @patch("posthog.ai.otel.processor.OTLPSpanExporter")
    @patch("posthog.ai.otel.processor.BatchSpanProcessor")
    def test_force_flush_without_timeout(self, mock_batch_cls, mock_otlp_cls):
        processor = PostHogSpanProcessor(api_key="phc_test")
        inner = mock_batch_cls.return_value

        processor.force_flush()
        inner.force_flush.assert_called_once_with()
