import unittest
from queue import Queue
from unittest import mock

from parameterized import parameterized

from posthog.client import Client
from posthog.consumer import AI_MAX_MSG_SIZE, Consumer
from posthog.request import AI_EVENTS_ENDPOINT, EVENTS_ENDPOINT
from posthog.test.test_utils import TEST_API_KEY


def _event(name: str) -> dict:
    return {"type": "capture", "event": name, "distinct_id": "distinct_id"}


class TestDedicatedAiEndpointConsumer(unittest.TestCase):
    def test_routes_ai_and_analytics_to_separate_endpoints(self) -> None:
        consumer = Consumer(None, TEST_API_KEY, dedicated_ai_endpoint=True)
        with mock.patch("posthog.consumer.batch_post") as mock_post:
            consumer.request([_event("$ai_generation"), _event("button_clicked")])

        by_path = {
            c.kwargs["path"]: c.kwargs["batch"] for c in mock_post.call_args_list
        }
        self.assertEqual(set(by_path), {EVENTS_ENDPOINT, AI_EVENTS_ENDPOINT})
        self.assertEqual(
            [e["event"] for e in by_path[AI_EVENTS_ENDPOINT]], ["$ai_generation"]
        )
        self.assertEqual(
            [e["event"] for e in by_path[EVENTS_ENDPOINT]], ["button_clicked"]
        )

    def test_only_ai_events_single_call_to_ai_endpoint(self) -> None:
        consumer = Consumer(None, TEST_API_KEY, dedicated_ai_endpoint=True)
        with mock.patch("posthog.consumer.batch_post") as mock_post:
            consumer.request([_event("$ai_generation"), _event("$ai_embedding")])

        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(mock_post.call_args.kwargs["path"], AI_EVENTS_ENDPOINT)

    def test_disabled_routes_everything_to_batch(self) -> None:
        consumer = Consumer(None, TEST_API_KEY, dedicated_ai_endpoint=False)
        with mock.patch("posthog.consumer.batch_post") as mock_post:
            consumer.request([_event("$ai_generation"), _event("button_clicked")])

        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(mock_post.call_args.kwargs["path"], EVENTS_ENDPOINT)


class TestDedicatedAiEndpointSyncMode(unittest.TestCase):
    @parameterized.expand(
        [
            ("enabled_ai_event", True, "$ai_generation", AI_EVENTS_ENDPOINT),
            ("enabled_normal_event", True, "button_clicked", EVENTS_ENDPOINT),
            ("disabled_ai_event", False, "$ai_generation", EVENTS_ENDPOINT),
        ]
    )
    def test_routes_event_to_expected_endpoint(
        self, _name, enabled, event, expected_path
    ) -> None:
        client = Client(TEST_API_KEY, sync_mode=True, _dedicated_ai_endpoint=enabled)
        with mock.patch("posthog.client.batch_post") as mock_post:
            client.capture(event, distinct_id="distinct_id")

        self.assertEqual(mock_post.call_args.kwargs["path"], expected_path)


class TestDedicatedAiEndpointSizeLimit(unittest.TestCase):
    def _sized_event(self, name: str, payload_bytes: int) -> dict:
        event = _event(name)
        event["properties"] = {"p": "x" * payload_bytes}
        return event

    @parameterized.expand(
        [
            (
                "ai_event_under_ai_limit_kept",
                True,
                "$ai_generation",
                2 * 1024 * 1024,
                True,
            ),
            (
                "ai_event_over_ai_limit_dropped",
                True,
                "$ai_generation",
                AI_MAX_MSG_SIZE,
                False,
            ),
            (
                "analytics_event_over_analytics_limit_dropped",
                True,
                "button_clicked",
                2 * 1024 * 1024,
                False,
            ),
            (
                "ai_event_uses_analytics_limit_when_disabled",
                False,
                "$ai_generation",
                2 * 1024 * 1024,
                False,
            ),
        ]
    )
    def test_per_event_size_limit(
        self, _name, enabled, event, payload_bytes, expect_kept
    ) -> None:
        q = Queue()
        consumer = Consumer(
            q, TEST_API_KEY, dedicated_ai_endpoint=enabled, flush_interval=0.01
        )
        q.put(self._sized_event(event, payload_bytes))

        batch = consumer.next()

        if expect_kept:
            self.assertEqual([e["event"] for e in batch], [event])
        else:
            self.assertEqual(batch, [])
            self.assertTrue(q.empty())
            self.assertEqual(q.unfinished_tasks, 0)


if __name__ == "__main__":
    unittest.main()
