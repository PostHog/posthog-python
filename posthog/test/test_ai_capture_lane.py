import threading
import unittest
from unittest import mock

import posthog

from posthog.ai.utils import _capture_ai_event
from posthog.capture_mode import CaptureMode
from posthog.client import Client
from posthog.consumer import AI_MAX_MSG_SIZE, MAX_MSG_SIZE
from posthog.request import AI_EVENTS_ENDPOINT, EVENTS_ENDPOINT
from posthog.test.test_utils import TEST_API_KEY


def _events_by_path(mock_post):
    by_path: dict[str, list] = {}
    for call in mock_post.call_args_list:
        by_path.setdefault(call.kwargs["path"], []).extend(call.kwargs["batch"])
    return by_path


class TestLaneRouting(unittest.TestCase):
    def _client(self, **kwargs):
        client = Client(TEST_API_KEY, flush_interval=0.05, **kwargs)
        self.addCleanup(client.join)
        return client

    def test_capture_ai_and_capture_ride_separate_lanes(self):
        client = self._client()
        with mock.patch("posthog.consumer.batch_post") as mock_post:
            client.capture("button_clicked", distinct_id="d")
            client._capture_ai("$ai_generation", distinct_id="d")
            client.flush()

        by_path = _events_by_path(mock_post)
        self.assertEqual(set(by_path), {EVENTS_ENDPOINT, AI_EVENTS_ENDPOINT})
        self.assertEqual(
            [e["event"] for e in by_path[EVENTS_ENDPOINT]], ["button_clicked"]
        )
        self.assertEqual(
            [e["event"] for e in by_path[AI_EVENTS_ENDPOINT]], ["$ai_generation"]
        )
        for call in mock_post.call_args_list:
            events = {e["event"] for e in call.kwargs["batch"]}
            expected = (
                {"$ai_generation"}
                if call.kwargs["path"] == AI_EVENTS_ENDPOINT
                else {"button_clicked"}
            )
            self.assertEqual(events, expected)

    def test_capture_does_not_reroute_ai_named_events(self):
        # The two-lane rule: `capture()` never special-cases AI events, no
        # matter their name. Only `_capture_ai()` reaches the AI lane.
        client = self._client()
        with mock.patch("posthog.consumer.batch_post") as mock_post:
            client.capture("$ai_generation", distinct_id="d")
            client.flush()

        self.assertEqual(
            [call.kwargs["path"] for call in mock_post.call_args_list],
            [EVENTS_ENDPOINT],
        )

    def test_capture_ai_returns_event_uuid_like_capture(self):
        client = self._client(send=False)
        uuid = client._capture_ai("$ai_generation", distinct_id="d")
        self.assertIsNotNone(uuid)

    def test_sync_mode_capture_ai_posts_single_event_batch_to_ai_endpoint(self):
        client = Client(TEST_API_KEY, sync_mode=True)
        with mock.patch("posthog.client.batch_post") as mock_post:
            client._capture_ai("$ai_generation", distinct_id="d")

        mock_post.assert_called_once()
        self.assertEqual(mock_post.call_args.kwargs["path"], AI_EVENTS_ENDPOINT)
        batch = mock_post.call_args.kwargs["batch"]
        self.assertEqual([e["event"] for e in batch], ["$ai_generation"])

    def test_multimodal_client_routes_wrapper_captures_to_ai_lane(self):
        client = self._client(_enable_multimodal_capture=True)
        with mock.patch("posthog.consumer.batch_post") as mock_post:
            _capture_ai_event(client, "$ai_generation", distinct_id="d")
            client.flush()
        by_path = _events_by_path(mock_post)
        self.assertEqual(set(by_path), {AI_EVENTS_ENDPOINT})

    def test_disabled_client_never_starts_ai_lane(self):
        client = Client(TEST_API_KEY, disabled=True)
        client._capture_ai("$ai_generation", distinct_id="d")
        self.assertEqual(client._ai_lane.consumers, [])

    def test_posthog_alias_accepts_private_kwargs(self):
        client = posthog.Posthog(
            TEST_API_KEY,
            send=False,
            _use_ai_lane=True,
            _enable_multimodal_capture=True,
        )
        self.assertTrue(client._use_ai_lane)
        self.assertTrue(client._enable_multimodal_capture)


class TestAnalyticsLaneUnchanged(unittest.TestCase):
    """Blast-radius guard: the analytics lane keeps today's wire behavior."""

    def test_analytics_consumers_keep_todays_parameters(self):
        client = Client(
            TEST_API_KEY,
            send=False,
            thread=2,
            flush_at=7,
            flush_interval=0.5,
            gzip=True,
            max_retries=4,
            timeout=9,
            historical_migration=True,
        )
        consumers = client.consumers
        self.assertEqual(len(consumers), 2)
        for consumer in consumers:
            self.assertIs(consumer.queue, client.queue)
            self.assertEqual(consumer.endpoint, EVENTS_ENDPOINT)
            self.assertEqual(consumer.max_msg_size, MAX_MSG_SIZE)
            self.assertEqual(consumer.flush_at, 7)
            self.assertEqual(consumer.flush_interval, 0.5)
            self.assertTrue(consumer.gzip)
            self.assertEqual(consumer.retries, 4)
            self.assertEqual(consumer.timeout, 9)
            self.assertTrue(consumer.historical_migration)
            self.assertEqual(consumer.capture_mode, client.capture_mode)
            self.assertEqual(consumer.capture_compression, client.capture_compression)
        client.join()

    def test_analytics_traffic_posts_to_single_endpoint(self):
        client = Client(TEST_API_KEY, flush_interval=0.05)
        with mock.patch("posthog.consumer.batch_post") as mock_post:
            client.capture("event_a", distinct_id="d")
            client.capture("event_b", distinct_id="d")
            client.flush()

        by_path = _events_by_path(mock_post)
        self.assertEqual(set(by_path), {EVENTS_ENDPOINT})
        self.assertEqual(
            sorted(e["event"] for e in by_path[EVENTS_ENDPOINT]),
            ["event_a", "event_b"],
        )
        client.join()

    def test_sync_mode_analytics_path_unchanged(self):
        client = Client(TEST_API_KEY, sync_mode=True)
        self.assertIsNone(client.consumers)
        with mock.patch("posthog.client.batch_post") as mock_post:
            client.capture("button_clicked", distinct_id="d")
        self.assertEqual(mock_post.call_args.kwargs["path"], EVENTS_ENDPOINT)


class TestLaneSizeCaps(unittest.TestCase):
    def _client(self):
        return Client(TEST_API_KEY, send=False, flush_interval=0.05)

    def _sized_event(self, name: str, payload_bytes: int) -> dict:
        return {
            "event": name,
            "distinct_id": "distinct_id",
            "properties": {"p": "x" * payload_bytes},
        }

    def test_ai_lane_accepts_multi_megabyte_events(self):
        client = self._client()
        client._ai_lane.start()
        consumer = client._ai_lane.consumers[0]
        client._ai_lane.queue.put(self._sized_event("$ai_generation", 2 * 1024 * 1024))
        batch = consumer.next()
        self.assertEqual([e["event"] for e in batch], ["$ai_generation"])

    def test_ai_lane_drops_events_over_its_cap(self):
        client = self._client()
        client._ai_lane.start()
        consumer = client._ai_lane.consumers[0]
        client._ai_lane.queue.put(self._sized_event("$ai_generation", AI_MAX_MSG_SIZE))
        self.assertEqual(consumer.next(), [])
        self.assertTrue(client._ai_lane.queue.empty())

    def test_analytics_lane_rejects_events_over_900kib(self):
        client = self._client()
        consumer = client.consumers[0]
        client.queue.put(self._sized_event("big_analytics_event", 2 * 1024 * 1024))
        self.assertEqual(consumer.next(), [])
        self.assertTrue(client.queue.empty())


class TestAiLaneV0Pinned(unittest.TestCase):
    """The AI endpoint has no v1 form: the AI lane ignores `capture_mode=v1`."""

    def test_ai_lane_consumers_pin_v0_and_ai_endpoint(self):
        client = Client(TEST_API_KEY, send=False, capture_mode="v1", thread=2)
        client._ai_lane.start()
        self.assertEqual(len(client._ai_lane.consumers), 2)
        for consumer in client._ai_lane.consumers:
            self.assertIs(consumer.queue, client._ai_lane.queue)
            self.assertEqual(consumer.endpoint, AI_EVENTS_ENDPOINT)
            self.assertEqual(consumer.max_msg_size, AI_MAX_MSG_SIZE)
            self.assertEqual(consumer.capture_mode, CaptureMode.V0)

    def test_async_ai_events_use_v0_even_with_capture_mode_v1(self):
        client = Client(TEST_API_KEY, capture_mode="v1", flush_interval=0.05)
        with (
            mock.patch("posthog.consumer.batch_post") as mock_post,
            mock.patch("posthog.consumer._send_v1_batch") as mock_v1,
        ):
            client._capture_ai("$ai_generation", distinct_id="d")
            client.capture("button_clicked", distinct_id="d")
            client.flush()

        mock_v1.assert_called()
        self.assertEqual(
            [call.kwargs["path"] for call in mock_post.call_args_list],
            [AI_EVENTS_ENDPOINT],
        )
        client.join()

    def test_sync_ai_events_use_v0_even_with_capture_mode_v1(self):
        client = Client(TEST_API_KEY, sync_mode=True, capture_mode="v1")
        with (
            mock.patch("posthog.client.batch_post") as mock_post,
            mock.patch("posthog.client._send_v1_batch") as mock_v1,
        ):
            client._capture_ai("$ai_generation", distinct_id="d")
            client.capture("button_clicked", distinct_id="d")

        mock_post.assert_called_once()
        self.assertEqual(mock_post.call_args.kwargs["path"], AI_EVENTS_ENDPOINT)
        mock_v1.assert_called_once()


class TestAiLaneLazyStart(unittest.TestCase):
    def test_no_ai_consumers_until_first_capture_ai(self):
        client = Client(TEST_API_KEY, flush_interval=0.05)
        self.assertEqual(client._ai_lane.consumers, [])

        with mock.patch("posthog.consumer.batch_post"):
            client.capture("button_clicked", distinct_id="d")
            client.flush()
        self.assertEqual(client._ai_lane.consumers, [])

        with mock.patch("posthog.consumer.batch_post"):
            client._capture_ai("$ai_generation", distinct_id="d")
            self.assertEqual(len(client._ai_lane.consumers), 1)
            self.assertTrue(client._ai_lane.consumers[0].is_alive())
            client.flush()
        client.join()

    def test_concurrent_first_captures_start_exactly_one_pool(self):
        client = Client(TEST_API_KEY, flush_interval=0.05)
        barrier = threading.Barrier(8)

        def fire():
            barrier.wait()
            client._capture_ai("$ai_generation", distinct_id="d")

        threads = [threading.Thread(target=fire) for _ in range(8)]
        with mock.patch("posthog.consumer.batch_post"):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            client.flush()

        self.assertEqual(len(client._ai_lane.consumers), 1)
        client.join()

    def test_flush_and_shutdown_noop_on_never_started_lane(self):
        client = Client(TEST_API_KEY, flush_interval=0.05)
        self.assertEqual(client._ai_lane.consumers, [])
        client.flush()
        client.shutdown()
        self.assertEqual(client._ai_lane.consumers, [])


class TestLaneForkRebuild(unittest.TestCase):
    def test_fork_rebuild_restarts_analytics_and_resets_ai(self):
        client = Client(
            TEST_API_KEY, flush_interval=0.05, enable_local_evaluation=False
        )
        with mock.patch("posthog.consumer.batch_post"):
            client._capture_ai("$ai_generation", distinct_id="d")
            client.flush()
        self.assertEqual(len(client._ai_lane.consumers), 1)

        old_consumers = list(client.consumers)
        old_analytics_queue = client._analytics_lane.queue
        old_ai_queue = client._ai_lane.queue

        client._reinit_after_fork()
        # Not actually forked: silence the parent's inherited consumer threads.
        for consumer in old_consumers:
            consumer.pause()

        self.assertIsNot(client._analytics_lane.queue, old_analytics_queue)
        self.assertIsNot(client._ai_lane.queue, old_ai_queue)
        self.assertEqual(len(client._analytics_lane.consumers), 1)
        self.assertTrue(client._analytics_lane.consumers[0].is_alive())
        self.assertEqual(client._ai_lane.consumers, [])

        with mock.patch("posthog.consumer.batch_post") as mock_post:
            client.capture("button_clicked", distinct_id="d")
            client._capture_ai("$ai_generation", distinct_id="d")
            client.flush()

        self.assertEqual(len(client._ai_lane.consumers), 1)
        self.assertEqual(
            set(_events_by_path(mock_post)), {EVENTS_ENDPOINT, AI_EVENTS_ENDPOINT}
        )
        client.join()

    def test_fork_rebuild_noop_for_sync_mode(self):
        client = Client(TEST_API_KEY, sync_mode=True)
        old_analytics_queue = client._analytics_lane.queue
        old_ai_queue = client._ai_lane.queue

        client._reinit_after_fork()

        self.assertIs(client._analytics_lane.queue, old_analytics_queue)
        self.assertIs(client._ai_lane.queue, old_ai_queue)


class TestCaptureAiEventHelper(unittest.TestCase):
    """`_capture_ai_event` rides the AI lane only when the client opted in."""

    def test_opted_in_routes_through_ai_lane(self):
        client = Client(TEST_API_KEY, flush_interval=0.05, _use_ai_lane=True)
        with mock.patch("posthog.consumer.batch_post") as mock_post:
            _capture_ai_event(
                client,
                "$ai_generation",
                distinct_id="d",
                properties={"x": 1},
            )
            client.flush()

        self.assertEqual(
            [call.kwargs["path"] for call in mock_post.call_args_list],
            [AI_EVENTS_ENDPOINT],
        )
        client.join()

    def test_default_keeps_capture_path(self):
        client = Client(TEST_API_KEY, flush_interval=0.05)
        with mock.patch("posthog.consumer.batch_post") as mock_post:
            _capture_ai_event(client, "$ai_generation", distinct_id="d")
            client.flush()

        self.assertEqual(
            [call.kwargs["path"] for call in mock_post.call_args_list],
            [EVENTS_ENDPOINT],
        )
        self.assertEqual(client._ai_lane.consumers, [])
        client.join()

    def test_default_mock_clients_keep_seeing_capture(self):
        # Downstream test suites pass Mock clients into the wrappers; without
        # the opt-in they must keep seeing plain `capture()` calls.
        client = mock.Mock()
        _capture_ai_event(client, "$ai_generation", distinct_id="d")
        client.capture.assert_called_once_with(event="$ai_generation", distinct_id="d")
        client._capture_ai.assert_not_called()

    def test_opted_in_prefers_capture_ai(self):
        client = mock.Mock(spec=["capture", "_capture_ai", "_use_ai_lane"])
        client._use_ai_lane = True
        _capture_ai_event(client, "$ai_generation", distinct_id="d")
        client._capture_ai.assert_called_once_with(
            event="$ai_generation", distinct_id="d"
        )
        client.capture.assert_not_called()

    def test_opted_in_duck_typed_client_without_method_falls_back(self):
        client = mock.Mock(spec=["capture", "_use_ai_lane"])
        client._use_ai_lane = True
        _capture_ai_event(client, "$ai_generation", distinct_id="d")
        client.capture.assert_called_once_with(event="$ai_generation", distinct_id="d")

    def test_client_multimodal_flag_prefers_capture_ai(self):
        client = mock.Mock(
            spec=["capture", "_capture_ai", "_enable_multimodal_capture"]
        )
        client._enable_multimodal_capture = True
        _capture_ai_event(client, "$ai_generation", distinct_id="d")
        client._capture_ai.assert_called_once_with(
            event="$ai_generation", distinct_id="d"
        )
        client.capture.assert_not_called()

    def test_client_multimodal_flag_off_keeps_capture(self):
        client = mock.Mock(
            spec=["capture", "_capture_ai", "_enable_multimodal_capture"]
        )
        client._enable_multimodal_capture = False
        _capture_ai_event(client, "$ai_generation", distinct_id="d")
        client.capture.assert_called_once_with(event="$ai_generation", distinct_id="d")


class TestLanesRefuseWorkAfterShutdown(unittest.TestCase):
    """`shutdown()` is terminal: no lane may accept events or start consumers
    afterwards, even a lazy AI lane that never started before shutdown."""

    def test_late_ai_capture_after_shutdown_starts_nothing_and_sends_nothing(self):
        client = Client(TEST_API_KEY, _use_ai_lane=True, flush_interval=0.05)
        client.shutdown()
        with mock.patch("posthog.consumer.batch_post") as mock_post:
            _capture_ai_event(client, "$ai_generation", distinct_id="d")
            client._ai_lane.queue.join()
        self.assertEqual(client._ai_lane.consumers, [])
        mock_post.assert_not_called()

    def test_late_analytics_capture_after_shutdown_drops_with_warning(self):
        client = Client(TEST_API_KEY, flush_interval=0.05)
        client.shutdown()
        with self.assertLogs("posthog", level="WARNING") as logs:
            uuid = client.capture("button_clicked", distinct_id="d")
        self.assertIsNone(uuid)
        self.assertIn("after shutdown", logs.output[0])
        self.assertTrue(client.queue.empty())


class TestModuleLevelFlagConfig(unittest.TestCase):
    """The lazily auto-instantiated default client picks the private AI flags
    up from module attributes, so deployments configuring PostHog via
    `posthog.<attr> = ...` never need to construct or mutate a client."""

    def setUp(self):
        self._saved = {
            "default_client": posthog.default_client,
            "api_key": posthog.api_key,
            "send": posthog.send,
        }
        posthog.default_client = None
        posthog.api_key = TEST_API_KEY
        posthog.send = False

    def tearDown(self):
        posthog._use_ai_lane = False
        posthog._enable_multimodal_capture = False
        posthog.default_client = self._saved["default_client"]
        posthog.api_key = self._saved["api_key"]
        posthog.send = self._saved["send"]

    def test_setup_applies_module_flags_to_new_default_client(self):
        posthog._use_ai_lane = True
        client = posthog.setup()
        self.assertTrue(client._use_ai_lane)
        self.assertFalse(client._enable_multimodal_capture)

    def test_setup_resyncs_flags_on_existing_default_client(self):
        client = posthog.setup()
        self.assertFalse(client._use_ai_lane)

        posthog._use_ai_lane = True
        posthog._enable_multimodal_capture = True
        self.assertIs(posthog.setup(), client)
        self.assertTrue(client._use_ai_lane)
        self.assertTrue(client._enable_multimodal_capture)


if __name__ == "__main__":
    unittest.main()
