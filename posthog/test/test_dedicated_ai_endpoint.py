import unittest
from unittest import mock

from posthog.client import Client
from posthog.consumer import Consumer
from posthog.request import AI_EVENTS_ENDPOINT, EVENTS_ENDPOINT
from posthog.test.test_utils import TEST_API_KEY


def _event(name: str) -> dict:
    return {"type": "capture", "event": name, "distinct_id": "distinct_id"}


class TestDedicatedAiEndpointConsumer(unittest.TestCase):
    def test_routes_ai_and_analytics_to_separate_endpoints(self) -> None:
        consumer = Consumer(None, TEST_API_KEY, dedicated_ai_endpoint=True)
        with mock.patch("posthog.consumer.batch_post") as mock_post:
            consumer.request([_event("$ai_generation"), _event("button_clicked")])

        by_path = {c.kwargs["path"]: c.kwargs["batch"] for c in mock_post.call_args_list}
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
    def test_routes_ai_event_to_ai_endpoint(self) -> None:
        client = Client(
            TEST_API_KEY, sync_mode=True, _internal_dedicated_ai_endpoint=True
        )
        with mock.patch("posthog.client.batch_post") as mock_post:
            client.capture("$ai_generation", distinct_id="distinct_id")

        self.assertEqual(mock_post.call_args.kwargs["path"], AI_EVENTS_ENDPOINT)

    def test_routes_normal_event_to_batch(self) -> None:
        client = Client(
            TEST_API_KEY, sync_mode=True, _internal_dedicated_ai_endpoint=True
        )
        with mock.patch("posthog.client.batch_post") as mock_post:
            client.capture("button_clicked", distinct_id="distinct_id")

        self.assertEqual(mock_post.call_args.kwargs["path"], EVENTS_ENDPOINT)

    def test_disabled_routes_ai_event_to_batch(self) -> None:
        client = Client(TEST_API_KEY, sync_mode=True)
        with mock.patch("posthog.client.batch_post") as mock_post:
            client.capture("$ai_generation", distinct_id="distinct_id")

        self.assertEqual(mock_post.call_args.kwargs["path"], EVENTS_ENDPOINT)


if __name__ == "__main__":
    unittest.main()
