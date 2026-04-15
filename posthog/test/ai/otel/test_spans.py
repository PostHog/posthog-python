import unittest

from parameterized import parameterized

from posthog.ai.otel.spans import is_ai_span
from posthog.test.ai.otel.conftest import make_span


class TestIsAISpan(unittest.TestCase):
    @parameterized.expand(
        [
            ("gen_ai", "gen_ai.chat"),
            ("llm", "llm.call"),
            ("ai", "ai.completion"),
            ("traceloop", "traceloop.workflow"),
        ]
    )
    def test_matches_ai_name_prefix(self, _name, span_name):
        self.assertTrue(is_ai_span(make_span(span_name)))

    @parameterized.expand(
        [
            ("gen_ai", {"gen_ai.system": "openai"}),
            ("llm", {"llm.model": "gpt-4"}),
            ("ai", {"ai.provider": "anthropic"}),
            ("traceloop", {"traceloop.entity.name": "chain"}),
        ]
    )
    def test_matches_ai_attribute_key(self, _name, attrs):
        self.assertTrue(is_ai_span(make_span("http.request", attrs)))

    @parameterized.expand(
        [
            ("http", "http.request"),
            ("db", "db.query"),
            ("custom", "my_function"),
        ]
    )
    def test_rejects_non_ai_name(self, _name, span_name):
        self.assertFalse(is_ai_span(make_span(span_name)))

    def test_rejects_non_ai_attributes(self):
        span = make_span("http.request", {"http.method": "GET", "http.url": "/"})
        self.assertFalse(is_ai_span(span))

    def test_empty_span(self):
        self.assertFalse(is_ai_span(make_span("test", {})))

    def test_none_attributes(self):
        span = make_span("test")
        span.attributes = None
        self.assertFalse(is_ai_span(span))
