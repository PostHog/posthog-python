import unittest
from unittest.mock import MagicMock

from posthog.ai.otel.spans import is_ai_span


def _make_span(name: str = "test", attributes: dict | None = None) -> MagicMock:
    span = MagicMock()
    span.name = name
    span.attributes = attributes or {}
    return span


class TestIsAISpan(unittest.TestCase):
    def test_matches_gen_ai_name_prefix(self):
        self.assertTrue(is_ai_span(_make_span("gen_ai.chat")))

    def test_matches_llm_name_prefix(self):
        self.assertTrue(is_ai_span(_make_span("llm.call")))

    def test_matches_ai_name_prefix(self):
        self.assertTrue(is_ai_span(_make_span("ai.completion")))

    def test_matches_traceloop_name_prefix(self):
        self.assertTrue(is_ai_span(_make_span("traceloop.workflow")))

    def test_rejects_non_ai_name(self):
        self.assertFalse(is_ai_span(_make_span("http.request")))
        self.assertFalse(is_ai_span(_make_span("db.query")))
        self.assertFalse(is_ai_span(_make_span("my_function")))

    def test_matches_gen_ai_attribute_key(self):
        span = _make_span("http.request", {"gen_ai.system": "openai"})
        self.assertTrue(is_ai_span(span))

    def test_matches_llm_attribute_key(self):
        span = _make_span("http.request", {"llm.model": "gpt-4"})
        self.assertTrue(is_ai_span(span))

    def test_matches_ai_attribute_key(self):
        span = _make_span("http.request", {"ai.provider": "anthropic"})
        self.assertTrue(is_ai_span(span))

    def test_matches_traceloop_attribute_key(self):
        span = _make_span("http.request", {"traceloop.entity.name": "chain"})
        self.assertTrue(is_ai_span(span))

    def test_rejects_non_ai_attributes(self):
        span = _make_span("http.request", {"http.method": "GET", "http.url": "/"})
        self.assertFalse(is_ai_span(span))

    def test_empty_span(self):
        self.assertFalse(is_ai_span(_make_span("test", {})))

    def test_none_attributes(self):
        span = _make_span("test")
        span.attributes = None
        self.assertFalse(is_ai_span(span))
