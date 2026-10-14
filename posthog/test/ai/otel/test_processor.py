import gc
import json
import threading
import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from opentelemetry.sdk.trace import TracerProvider

from posthog import contexts
from posthog.ai.otel.processor import PostHogSpanProcessor, _reset_processors_after_fork
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

        for result in (True, False):
            with self.subTest(result=result):
                inner.force_flush.reset_mock()
                inner.force_flush.return_value = result
                self.assertIs(processor.force_flush(timeout_millis=5000), result)
                inner.force_flush.assert_called_once_with(5000)

    @patch("posthog.ai.otel.processor.OTLPSpanExporter")
    @patch("posthog.ai.otel.processor.BatchSpanProcessor")
    def test_force_flush_without_timeout(self, mock_batch_cls, mock_otlp_cls):
        processor = PostHogSpanProcessor(api_key="phc_test")
        inner = mock_batch_cls.return_value

        for result in (True, False):
            with self.subTest(result=result):
                inner.force_flush.reset_mock()
                inner.force_flush.return_value = result
                self.assertIs(processor.force_flush(), result)
                inner.force_flush.assert_called_once_with()


def new_request_context():
    return contexts.new_context(fresh=True, capture_exceptions=False)


class TestPostHogSpanProcessorRequestContext(unittest.TestCase):
    def setUp(self):
        exporter_patch = patch("posthog.ai.otel.processor.OTLPSpanExporter")
        batch_patch = patch("posthog.ai.otel.processor.BatchSpanProcessor")
        exporter_patch.start()
        self.inner = batch_patch.start().return_value
        self.addCleanup(exporter_patch.stop)
        self.addCleanup(batch_patch.stop)
        provider = TracerProvider()
        self.processor = PostHogSpanProcessor(api_key="phc_test")
        provider.add_span_processor(self.processor)
        self.other_processor = MagicMock()
        provider.add_span_processor(self.other_processor)
        self.tracer = provider.get_tracer("test")

    def _emit_span(self, name="gen_ai.chat", attributes=None):
        with self.tracer.start_as_current_span(name, attributes=attributes):
            pass

    def _exported_attributes(self):
        return [
            dict(call.args[0].attributes) for call in self.inner.on_end.call_args_list
        ]

    def test_exports_distinct_id_session_and_tags(self):
        with new_request_context():
            contexts.identify_context("alice")
            contexts.set_context_session("s_alice")
            contexts.tag("plan", "pro")
            self._emit_span()

        self.assertEqual(
            self._exported_attributes(),
            [
                {
                    "posthog.distinct_id": "alice",
                    "$session_id": "s_alice",
                    "plan": "pro",
                }
            ],
        )

    def test_other_processors_do_not_see_the_context(self):
        with new_request_context():
            contexts.identify_context("alice")
            contexts.tag("plan", "pro")
            self._emit_span(attributes={"gen_ai.system": "openai"})

        started = self.other_processor.on_start.call_args.args[0]
        ended = self.other_processor.on_end.call_args.args[0]
        self.assertEqual(dict(started.attributes), {"gen_ai.system": "openai"})
        self.assertEqual(dict(ended.attributes), {"gen_ai.system": "openai"})

    def test_drops_non_ai_spans_started_in_a_context(self):
        with new_request_context():
            contexts.identify_context("alice")
            self._emit_span(name="http.request")

        self.inner.on_end.assert_not_called()
        self.assertEqual(self.processor._captured, {})

    def test_shutdown_clears_captured_contexts(self):
        with new_request_context():
            contexts.identify_context("alice")
            with self.tracer.start_as_current_span("gen_ai.chat"):
                self.assertEqual(len(self.processor._captured), 1)
                self.processor.shutdown()
                self.assertEqual(self.processor._captured, {})

    def test_keeps_attributes_already_on_the_span(self):
        with new_request_context():
            contexts.set_context_session("s_alice")
            self._emit_span(attributes={"$session_id": "conv_42"})

        self.assertEqual(self._exported_attributes()[0]["$session_id"], "conv_42")

    def test_skips_context_distinct_id_when_span_has_an_identity(self):
        identity_keys = (
            "posthog.distinct_id",
            "user.id",
            "ai.telemetry.metadata.posthog_distinct_id",
            "ai.settings.context.posthog_distinct_id",
        )
        for key in identity_keys:
            with self.subTest(key=key):
                self.inner.on_end.reset_mock()
                with new_request_context():
                    contexts.identify_context("alice")
                    self._emit_span(attributes={key: "bob"})

                attributes = self._exported_attributes()[0]
                self.assertEqual(attributes[key], "bob")
                if key != "posthog.distinct_id":
                    self.assertNotIn("posthog.distinct_id", attributes)

    def test_skips_distinct_id_tag_when_span_has_an_identity(self):
        with new_request_context():
            contexts.tag("posthog.distinct_id", "alice")
            self._emit_span(attributes={"user.id": "bob"})

        attributes = self._exported_attributes()[0]
        self.assertEqual(attributes, {"user.id": "bob"})

    def test_encodes_structured_tags_as_json(self):
        with new_request_context():
            contexts.tag("account", {"tier": "pro", "seats": 3})
            contexts.tag("arms", ["short-prompt", "new-router"])
            contexts.tag("mixed", ["a", 1])
            contexts.tag("nested_not_finite", {"x": float("inf")})
            self._emit_span()

        attributes = self._exported_attributes()[0]
        self.assertEqual(json.loads(attributes["account"]), {"tier": "pro", "seats": 3})
        self.assertEqual(list(attributes["arms"]), ["short-prompt", "new-router"])
        self.assertEqual(json.loads(attributes["mixed"]), ["a", 1])
        self.assertNotIn("nested_not_finite", attributes)

    def test_skips_non_finite_numbers(self):
        with new_request_context():
            contexts.tag("nan", float("nan"))
            contexts.tag("inf", float("-inf"))
            contexts.tag("list_with_inf", [1.0, float("inf")])
            contexts.tag("plan", "pro")
            self._emit_span()

        self.assertEqual(self._exported_attributes(), [{"plan": "pro"}])

    def test_string_like_values_arrive_as_plain_strings(self):
        moment = datetime(2026, 10, 14, 8, 30, tzinfo=timezone.utc)
        identifier = uuid.UUID("12345678-1234-5678-1234-567812345678")
        with new_request_context():
            contexts.tag("when", moment)
            contexts.tag("id", identifier)
            contexts.tag("price", Decimal("1.50"))
            self._emit_span()

        self.assertEqual(
            self._exported_attributes(),
            [{"when": moment.isoformat(), "id": str(identifier), "price": "1.50"}],
        )

    def test_skips_values_json_cannot_represent(self):
        with new_request_context():
            contexts.tag("tags", {"a", "b"})
            contexts.tag("raw", b"bytes")
            contexts.tag("plan", "pro")
            self._emit_span()

        self.assertEqual(self._exported_attributes(), [{"plan": "pro"}])

    def test_tags_win_over_context_ids_and_survive_an_empty_context(self):
        with new_request_context():
            contexts.tag("$session_id", "conv_42")
            self._emit_span()
        with new_request_context():
            contexts.set_context_session("s_alice")
            contexts.tag("$session_id", "conv_42")
            self._emit_span()

        self.assertEqual(
            [a["$session_id"] for a in self._exported_attributes()],
            ["conv_42", "conv_42"],
        )

    def test_explicit_ai_session_id_tag_passes_through(self):
        with new_request_context():
            contexts.set_context_session("s_alice")
            contexts.tag("$ai_session_id", "conv_42")
            self._emit_span()

        attributes = self._exported_attributes()[0]
        self.assertEqual(attributes["$ai_session_id"], "conv_42")
        self.assertEqual(attributes["$session_id"], "s_alice")

    def test_context_session_does_not_set_ai_session_id(self):
        with new_request_context():
            contexts.set_context_session("s_alice")
            self._emit_span()

        self.assertNotIn("$ai_session_id", self._exported_attributes()[0])

    def test_integers_outside_signed_64_bit_range_are_sent_as_strings(self):
        with new_request_context():
            contexts.tag("big", 2**63)
            contexts.tag("small", -(2**63) - 1)
            contexts.tag("edge", 2**63 - 1)
            contexts.tag("bigs", [2**64, 1])
            self._emit_span()

        attributes = self._exported_attributes()[0]
        self.assertEqual(attributes["big"], str(2**63))
        self.assertEqual(attributes["small"], str(-(2**63) - 1))
        self.assertEqual(attributes["edge"], 2**63 - 1)
        self.assertEqual(json.loads(attributes["bigs"]), [2**64, 1])

    def test_abandoned_span_entry_is_removed_when_garbage_collected(self):
        with new_request_context():
            contexts.identify_context("alice")
            span = self.tracer.start_span("gen_ai.chat")
        self.assertEqual(len(self.processor._captured), 1)

        self.other_processor.reset_mock()
        del span
        gc.collect()

        self.assertEqual(self.processor._captured, {})

    def test_collecting_an_abandoned_span_does_not_deadlock_on_the_held_lock(self):
        def collect_while_holding_lock():
            with new_request_context():
                contexts.identify_context("alice")
                holder = []
                span = self.tracer.start_span("gen_ai.chat")
                holder.append(span)
                holder.append(holder)
            del span, holder
            self.other_processor.reset_mock()
            with self.processor._captured_lock:
                gc.collect()

        worker = threading.Thread(target=collect_while_holding_lock, daemon=True)
        worker.start()
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive(), "gc deadlocked on the captured lock")
        self.assertEqual(self.processor._captured, {})

    def test_finalizer_is_detached_once_the_span_ends(self):
        with new_request_context():
            contexts.identify_context("alice")
            span = self.tracer.start_span("gen_ai.chat")
        _, finalizer = next(iter(self.processor._captured.values()))
        self.assertTrue(finalizer.alive)

        span.end()

        self.assertFalse(finalizer.alive)
        self.assertEqual(self.processor._captured, {})

    def test_after_fork_resets_lock_and_store(self):
        with new_request_context():
            contexts.identify_context("alice")
            span = self.tracer.start_span("gen_ai.chat")
        old_lock = self.processor._captured_lock

        _reset_processors_after_fork()

        self.assertIsNot(self.processor._captured_lock, old_lock)
        self.assertEqual(self.processor._captured, {})
        span.end()

    def test_unstampable_tag_does_not_drop_the_other_tags(self):
        class Unstampable(list):
            def __iter__(self):
                raise RuntimeError("cannot iterate")

        with new_request_context():
            contexts.tag("bad", Unstampable([1]))
            contexts.tag("plan", "pro")
            self._emit_span()

        self.assertEqual(self._exported_attributes(), [{"plan": "pro"}])

    def test_adds_nothing_without_a_context(self):
        self._emit_span()

        self.assertEqual(self._exported_attributes(), [{}])

    def test_overlapping_requests_export_their_own_values(self):
        both_started = threading.Barrier(2)

        def request(user):
            with new_request_context():
                contexts.identify_context(user)
                span = self.tracer.start_span("gen_ai.chat")
            both_started.wait(timeout=5)
            span.end()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(request, ["alice", "bob"]))

        self.assertEqual(
            sorted(a["posthog.distinct_id"] for a in self._exported_attributes()),
            ["alice", "bob"],
        )
