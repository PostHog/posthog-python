import os
import threading
import unittest
import uuid
from unittest import mock

from parameterized import parameterized

import posthog

from posthog.ai.utils import _capture_ai_event, finalize_ai_content, with_privacy_mode
from posthog.capture_compression import CAPTURE_COMPRESSION_ENV_VAR, CaptureCompression
from posthog.client import Client
from posthog.consumer import AI_MAX_MSG_SIZE, AI_MAX_PROPERTIES_SIZE, MAX_MSG_SIZE
from posthog.capture_send import _CAPTURE_AI_V1_PATH, _CAPTURE_V1_PATH
from posthog.version import VERSION
from posthog.test.capture_helpers import patch_capture_send, sent_batch
from posthog.test.test_utils import TEST_API_KEY


def _events_by_path(mock_send):
    by_path: dict[str, list] = {}
    for index, call in enumerate(mock_send.call_args_list):
        by_path.setdefault(call.kwargs["path"], []).extend(sent_batch(mock_send, index))
    return by_path


class TestLaneRouting(unittest.TestCase):
    def _client(self, **kwargs):
        client = Client(TEST_API_KEY, flush_interval=0.05, **kwargs)
        self.addCleanup(client.join)
        return client

    def test_capture_ai_and_capture_ride_separate_lanes(self):
        client = self._client()
        with patch_capture_send("consumer") as mock_post:
            client.capture("button_clicked", distinct_id="d")
            client.capture_ai("$ai_generation", distinct_id="d")
            client.flush()

        by_path = _events_by_path(mock_post)
        self.assertEqual(set(by_path), {_CAPTURE_V1_PATH, _CAPTURE_AI_V1_PATH})
        self.assertEqual(
            [e["event"] for e in by_path[_CAPTURE_V1_PATH]], ["button_clicked"]
        )
        self.assertEqual(
            [e["event"] for e in by_path[_CAPTURE_AI_V1_PATH]], ["$ai_generation"]
        )
        for index, call in enumerate(mock_post.call_args_list):
            events = {e["event"] for e in sent_batch(mock_post, index)}
            expected = (
                {"$ai_generation"}
                if call.kwargs["path"] == _CAPTURE_AI_V1_PATH
                else {"button_clicked"}
            )
            self.assertEqual(events, expected)

    def test_capture_does_not_reroute_ai_named_events(self):
        # The two-lane rule: `capture()` never special-cases AI events, no
        # matter their name. Only `capture_ai()` reaches the AI lane.
        client = self._client()
        with patch_capture_send("consumer") as mock_post:
            client.capture("$ai_generation", distinct_id="d")
            client.flush()

        self.assertEqual(
            [call.kwargs["path"] for call in mock_post.call_args_list],
            [_CAPTURE_V1_PATH],
        )

    def test_capture_ai_returns_event_uuid_like_capture(self):
        client = self._client(send=False)
        uuid = client.capture_ai("$ai_generation", distinct_id="d")
        self.assertIsNotNone(uuid)

    def test_sync_mode_capture_ai_posts_single_event_batch_to_ai_endpoint(self):
        client = Client(TEST_API_KEY, sync_mode=True)
        with patch_capture_send("client") as mock_post:
            client.capture_ai("$ai_generation", distinct_id="d")

        mock_post.assert_called_once()
        self.assertEqual(mock_post.call_args.kwargs["path"], _CAPTURE_AI_V1_PATH)
        batch = sent_batch(mock_post)
        self.assertEqual([e["event"] for e in batch], ["$ai_generation"])

    def test_disabled_client_never_starts_ai_lane(self):
        client = Client(TEST_API_KEY, disabled=True)
        client.capture_ai("$ai_generation", distinct_id="d")
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
            max_retries=4,
            timeout=9,
            historical_migration=True,
        )
        consumers = client.consumers
        self.assertEqual(len(consumers), 2)
        for consumer in consumers:
            self.assertIs(consumer.queue, client.queue)
            self.assertEqual(consumer.endpoint, _CAPTURE_V1_PATH)
            self.assertEqual(consumer.max_msg_size, MAX_MSG_SIZE)
            self.assertEqual(consumer.flush_at, 7)
            self.assertEqual(consumer.flush_interval, 0.5)
            self.assertEqual(consumer.retries, 4)
            self.assertEqual(consumer.timeout, 9)
            self.assertTrue(consumer.historical_migration)
            self.assertEqual(consumer.capture_compression, client.capture_compression)
        client.join()

    def test_analytics_traffic_posts_to_single_endpoint(self):
        client = Client(TEST_API_KEY, flush_interval=0.05)
        with patch_capture_send("consumer") as mock_post:
            client.capture("event_a", distinct_id="d")
            client.capture("event_b", distinct_id="d")
            client.flush()

        by_path = _events_by_path(mock_post)
        self.assertEqual(set(by_path), {_CAPTURE_V1_PATH})
        self.assertEqual(
            sorted(e["event"] for e in by_path[_CAPTURE_V1_PATH]),
            ["event_a", "event_b"],
        )
        client.join()

    def test_sync_mode_analytics_path_unchanged(self):
        client = Client(TEST_API_KEY, sync_mode=True)
        self.assertIsNone(client.consumers)
        with patch_capture_send("client") as mock_post:
            client.capture("button_clicked", distinct_id="d")
        self.assertEqual(mock_post.call_args.kwargs["path"], _CAPTURE_V1_PATH)


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

    @parameterized.expand(
        [
            ("properties_at_endpoint_ceiling", {}, AI_MAX_PROPERTIES_SIZE, True),
            ("over_guard", {}, AI_MAX_MSG_SIZE, False),
            (
                "over_lowered_cap",
                {"capture_ai_max_event_bytes": 1024 * 1024},
                2 * 1024 * 1024,
                False,
            ),
        ]
    )
    def test_ai_lane_size_guard(self, _name, config, payload_bytes, accepted):
        client = Client(TEST_API_KEY, send=False, flush_interval=0.05, **config)
        client._ai_lane.start()
        consumer = client._ai_lane.consumers[0]
        client._ai_lane.queue.put(self._sized_event("$ai_generation", payload_bytes))
        self.assertEqual(
            [e["event"] for e in consumer.next()],
            ["$ai_generation"] if accepted else [],
        )
        self.assertTrue(client._ai_lane.queue.empty())

    def test_sync_mode_ai_event_over_cap_is_not_sent(self):
        client = Client(
            TEST_API_KEY, sync_mode=True, capture_ai_max_event_bytes=1024 * 1024
        )
        with patch_capture_send("client") as mock_send:
            with self.assertLogs("posthog", level="ERROR") as logs:
                result = client.capture_ai(
                    "$ai_generation",
                    distinct_id="d",
                    properties={"p": "x" * (2 * 1024 * 1024)},
                )

        self.assertIsNone(result)
        mock_send.assert_not_called()
        self.assertIn("exceeds the 1024KiB limit", "\n".join(logs.output))

    def test_analytics_lane_rejects_events_over_900kib(self):
        client = self._client()
        consumer = client.consumers[0]
        client.queue.put(self._sized_event("big_analytics_event", 2 * 1024 * 1024))
        self.assertEqual(consumer.next(), [])
        self.assertTrue(client.queue.empty())


class TestAiLaneWireConfig(unittest.TestCase):
    """The AI lane has its own endpoint, compression, timeout, queue and size
    guard, independent of the analytics lane's settings."""

    @parameterized.expand(
        [
            ("defaults", {}, CaptureCompression.NONE, 30, 1000, AI_MAX_MSG_SIZE),
            (
                "configured",
                {
                    "capture_ai_compression": "zstd",
                    "capture_ai_timeout": 45,
                    "capture_ai_max_queue_size": 50,
                    "capture_ai_max_event_bytes": 1024 * 1024,
                },
                CaptureCompression.ZSTD,
                45,
                50,
                1024 * 1024,
            ),
        ]
    )
    def test_ai_lane_consumers_use_ai_config(
        self, _name, config, compression, timeout, queue_size, max_event_bytes
    ):
        with mock.patch.dict(os.environ, {CAPTURE_COMPRESSION_ENV_VAR: "deflate"}):
            client = Client(
                TEST_API_KEY,
                send=False,
                capture_compression="gzip",
                timeout=9,
                max_queue_size=77,
                thread=2,
                **config,
            )
        client._ai_lane.start()
        self.assertEqual(client._ai_lane.queue.maxsize, queue_size)
        self.assertEqual(len(client._ai_lane.consumers), 2)
        for consumer in client._ai_lane.consumers:
            self.assertIs(consumer.queue, client._ai_lane.queue)
            self.assertEqual(consumer.endpoint, _CAPTURE_AI_V1_PATH)
            self.assertEqual(consumer.max_msg_size, max_event_bytes)
            self.assertEqual(consumer.capture_compression, compression)
            self.assertEqual(consumer.timeout, timeout)
        self.assertEqual(client.queue.maxsize, 77)
        for consumer in client._analytics_lane.consumers:
            self.assertEqual(consumer.capture_compression, CaptureCompression.GZIP)
            self.assertEqual(consumer.timeout, 9)
        client.join()

    @parameterized.expand(
        [
            (
                "event_bytes_over_ceiling",
                "capture_ai_max_event_bytes",
                AI_MAX_MSG_SIZE + 1,
            ),
            ("event_bytes_zero", "capture_ai_max_event_bytes", 0),
            ("event_bytes_float", "capture_ai_max_event_bytes", 1024.5),
            ("event_bytes_bool", "capture_ai_max_event_bytes", True),
            ("queue_size_negative", "capture_ai_max_queue_size", -1),
            ("queue_size_string", "capture_ai_max_queue_size", "100"),
            ("timeout_zero", "capture_ai_timeout", 0),
            ("timeout_nan", "capture_ai_timeout", float("nan")),
            ("timeout_inf", "capture_ai_timeout", float("inf")),
            ("compression_unknown", "capture_ai_compression", "br"),
        ]
    )
    def test_invalid_ai_config_raises(self, _name, field, value):
        with self.assertRaisesRegex(ValueError, field):
            Client(TEST_API_KEY, send=False, **{field: value})

    def test_async_lanes_keep_separate_path_and_compression(self):
        client = Client(TEST_API_KEY, capture_compression="gzip", flush_interval=0.05)
        with patch_capture_send("consumer") as mock_send:
            client.capture_ai("$ai_generation", distinct_id="d")
            client.capture("button_clicked", distinct_id="d")
            client.flush()

        sends = {
            call.kwargs["path"]: call.kwargs["compression"]
            for call in mock_send.call_args_list
        }
        self.assertEqual(
            sends,
            {
                _CAPTURE_AI_V1_PATH: CaptureCompression.NONE,
                _CAPTURE_V1_PATH: CaptureCompression.GZIP,
            },
        )
        self.assertEqual(
            _events_by_path(mock_send)[_CAPTURE_AI_V1_PATH][0]["event"],
            "$ai_generation",
        )
        client.join()

    def test_sync_lanes_keep_separate_path_compression_and_timeout(self):
        client = Client(
            TEST_API_KEY,
            sync_mode=True,
            capture_compression="gzip",
            capture_ai_compression="deflate",
            timeout=9,
            capture_ai_timeout=45,
        )
        with patch_capture_send("client") as mock_send:
            client.capture_ai("$ai_generation", distinct_id="d")
            client.capture("button_clicked", distinct_id="d")

        self.assertEqual(
            [
                (
                    call.kwargs["path"],
                    call.kwargs["compression"],
                    call.kwargs["timeout"],
                )
                for call in mock_send.call_args_list
            ],
            [
                (_CAPTURE_AI_V1_PATH, CaptureCompression.DEFLATE, 45),
                (_CAPTURE_V1_PATH, CaptureCompression.GZIP, 9),
            ],
        )

    def test_flush_drains_ai_lane_while_waiting_on_analytics(self):
        release_analytics = threading.Event()
        ai_sent = threading.Event()

        def send(api_key, host, batch, **kwargs):
            if kwargs["path"] == _CAPTURE_AI_V1_PATH:
                ai_sent.set()
            else:
                release_analytics.wait(5)

        client = Client(TEST_API_KEY, flush_interval=30)
        with patch_capture_send("consumer", side_effect=send):
            client.capture("button_clicked", distinct_id="d")
            client.capture_ai("$ai_generation", distinct_id="d")
            flusher = threading.Thread(target=client.flush, args=(10,))
            flusher.start()
            try:
                self.assertTrue(ai_sent.wait(2))
            finally:
                release_analytics.set()
                flusher.join(5)
            client.join()


class TestAiLaneLazyStart(unittest.TestCase):
    def test_no_ai_consumers_until_first_capture_ai(self):
        client = Client(TEST_API_KEY, flush_interval=0.05)
        self.assertEqual(client._ai_lane.consumers, [])

        with patch_capture_send("consumer"):
            client.capture("button_clicked", distinct_id="d")
            client.flush()
        self.assertEqual(client._ai_lane.consumers, [])

        with patch_capture_send("consumer"):
            client.capture_ai("$ai_generation", distinct_id="d")
            self.assertEqual(len(client._ai_lane.consumers), 1)
            self.assertTrue(client._ai_lane.consumers[0].is_alive())
            client.flush()
        client.join()

    def test_concurrent_first_captures_start_exactly_one_pool(self):
        client = Client(TEST_API_KEY, flush_interval=0.05)
        barrier = threading.Barrier(8)

        def fire():
            barrier.wait()
            client.capture_ai("$ai_generation", distinct_id="d")

        threads = [threading.Thread(target=fire) for _ in range(8)]
        with patch_capture_send("consumer"):
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
        with patch_capture_send("consumer"):
            client.capture_ai("$ai_generation", distinct_id="d")
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

        with patch_capture_send("consumer") as mock_post:
            client.capture("button_clicked", distinct_id="d")
            client.capture_ai("$ai_generation", distinct_id="d")
            client.flush()

        self.assertEqual(len(client._ai_lane.consumers), 1)
        self.assertEqual(
            set(_events_by_path(mock_post)), {_CAPTURE_V1_PATH, _CAPTURE_AI_V1_PATH}
        )
        client.join()

    def test_fork_rebuild_replaces_sync_mode_queues(self):
        client = Client(TEST_API_KEY, sync_mode=True)
        old_analytics_queue = client._analytics_lane.queue
        old_ai_queue = client._ai_lane.queue

        client._reinit_after_fork()

        self.assertIsNot(client._analytics_lane.queue, old_analytics_queue)
        self.assertIsNot(client._ai_lane.queue, old_ai_queue)


class TestCaptureAiEventHelper(unittest.TestCase):
    """`_capture_ai_event` always rides the AI lane; `enable_full_ai_capture` only controls content."""

    @parameterized.expand([("default", False), ("full_capture", True)])
    def test_routes_only_through_ai_lane(self, _name, full_capture):
        client = Client(
            TEST_API_KEY, flush_interval=0.05, enable_full_ai_capture=full_capture
        )
        with patch_capture_send("consumer") as mock_post:
            _capture_ai_event(
                client,
                "$ai_generation",
                distinct_id="d",
                properties={"x": 1},
            )
            client.flush()

        self.assertEqual(
            [call.kwargs["path"] for call in mock_post.call_args_list],
            [_CAPTURE_AI_V1_PATH],
        )
        client.join()

    def test_adds_ai_library_identity_and_preserves_provider_and_model(self):
        client = mock.Mock()
        _capture_ai_event(
            client,
            "$ai_generation",
            distinct_id="d",
            properties={"$ai_provider": "openai", "$ai_model": "gpt-4o"},
        )

        properties = client.capture_ai.call_args.kwargs["properties"]
        self.assertEqual(properties["$ai_lib"], "posthog-ai")
        self.assertEqual(properties["$ai_lib_version"], VERSION)
        self.assertEqual(properties["$ai_provider"], "openai")
        self.assertEqual(properties["$ai_model"], "gpt-4o")

    @parameterized.expand([("default", False), ("full_capture", True)])
    def test_client_without_capture_ai_falls_back_to_capture(self, _name, full_capture):
        client = mock.Mock(spec=["capture", "enable_full_ai_capture"])
        client.enable_full_ai_capture = full_capture
        _capture_ai_event(client, "$ai_generation", distinct_id="d")
        client.capture.assert_called_once_with(
            event="$ai_generation",
            distinct_id="d",
            properties={"$ai_lib": "posthog-ai", "$ai_lib_version": VERSION},
        )


class TestLanesRefuseWorkAfterShutdown(unittest.TestCase):
    """`shutdown()` is terminal: no lane may accept events or start consumers
    afterwards, even a lazy AI lane that never started before shutdown."""

    def test_late_ai_capture_after_shutdown_starts_nothing_and_sends_nothing(self):
        client = Client(TEST_API_KEY, flush_interval=0.05)
        client.shutdown()
        with patch_capture_send("consumer") as mock_post:
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
    """The default client picks up the AI capture flag and its deprecated aliases from module attributes."""

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
        posthog.enable_full_ai_capture = False
        posthog._use_ai_lane = False
        posthog._enable_multimodal_capture = False
        posthog.default_client = self._saved["default_client"]
        posthog.api_key = self._saved["api_key"]
        posthog.send = self._saved["send"]

    def test_setup_applies_module_flags_to_new_default_client(self):
        posthog._use_ai_lane = True
        client = posthog.setup()
        self.assertTrue(client._use_ai_lane)
        self.assertTrue(client._enable_multimodal_capture)

    def test_setup_resyncs_flags_on_existing_default_client(self):
        client = posthog.setup()
        self.assertFalse(client._use_ai_lane)

        posthog._use_ai_lane = True
        posthog._enable_multimodal_capture = True
        self.assertIs(posthog.setup(), client)
        self.assertTrue(client._use_ai_lane)
        self.assertTrue(client._enable_multimodal_capture)


class TestFullAiCaptureFlag(unittest.TestCase):
    def _client(self, **kwargs):
        client = Client(TEST_API_KEY, flush_interval=0.05, **kwargs)
        self.addCleanup(client.join)
        return client

    def test_new_flag_routes_wrapper_captures_to_ai_lane(self):
        client = self._client(enable_full_ai_capture=True)
        with patch_capture_send("consumer") as mock_post:
            _capture_ai_event(client, "$ai_generation", distinct_id="d")
            client.flush()
        self.assertEqual(set(_events_by_path(mock_post)), {_CAPTURE_AI_V1_PATH})

    def test_deprecated_kwargs_map_to_new_flag(self):
        for kwargs in ({"_use_ai_lane": True}, {"_enable_multimodal_capture": True}):
            client = Client(TEST_API_KEY, send=False, **kwargs)
            self.addCleanup(client.join)
            self.assertTrue(client.enable_full_ai_capture)

    def test_alias_properties_read_and_write_the_new_flag(self):
        client = Client(TEST_API_KEY, send=False)
        self.addCleanup(client.join)
        self.assertFalse(client._use_ai_lane)
        self.assertFalse(client._enable_multimodal_capture)
        client._enable_multimodal_capture = True
        self.assertTrue(client.enable_full_ai_capture)
        self.assertTrue(client._use_ai_lane)

    def test_module_globals_sync_onto_default_client(self):
        previous = (
            posthog.default_client,
            posthog.project_api_key,
            posthog.enable_full_ai_capture,
            posthog._use_ai_lane,
        )
        try:
            posthog.default_client = Client(TEST_API_KEY, send=False)
            posthog.project_api_key = TEST_API_KEY
            posthog.enable_full_ai_capture = True
            posthog.setup()
            self.assertTrue(posthog.default_client.enable_full_ai_capture)
            posthog.enable_full_ai_capture = False
            posthog._use_ai_lane = True
            posthog.setup()
            self.assertTrue(posthog.default_client.enable_full_ai_capture)
        finally:
            (
                posthog.default_client,
                posthog.project_api_key,
                posthog.enable_full_ai_capture,
                posthog._use_ai_lane,
            ) = previous


class TestPublicCaptureAi(unittest.TestCase):
    def test_capture_ai_is_public_and_returns_uuid(self):
        client = Client(TEST_API_KEY, send=False)
        self.addCleanup(client.join)
        self.assertIsNotNone(client.capture_ai("$ai_generation", distinct_id="d"))
        self.assertFalse(hasattr(client, "_capture_ai"))

    def test_module_level_capture_ai_returns_uuid(self):
        previous = (posthog.default_client, posthog.project_api_key)
        try:
            posthog.default_client = Client(TEST_API_KEY, send=False)
            posthog.project_api_key = TEST_API_KEY
            self.assertIsNotNone(posthog.capture_ai("$ai_generation", distinct_id="d"))
        finally:
            posthog.default_client, posthog.project_api_key = previous


class TestCaptureAiUuid(unittest.TestCase):
    def _client(self, **kwargs):
        client = Client(TEST_API_KEY, flush_interval=0.05, **kwargs)
        self.addCleanup(client.join)
        return client

    def test_returned_uuid_matches_the_wire_event_uuid(self):
        client = self._client()
        with patch_capture_send("consumer") as mock_post:
            returned_uuid = client.capture_ai("$ai_generation", distinct_id="d")
            client.flush()

        batch = sent_batch(mock_post)
        self.assertEqual(batch[0]["uuid"], returned_uuid)

    def test_supplied_uuid_is_preserved_end_to_end(self):
        client = self._client()
        supplied_uuid = str(uuid.uuid4())
        with patch_capture_send("consumer") as mock_post:
            returned_uuid = client.capture_ai(
                "$ai_generation", distinct_id="d", uuid=supplied_uuid
            )
            client.flush()

        self.assertEqual(returned_uuid, supplied_uuid)
        batch = sent_batch(mock_post)
        self.assertEqual(batch[0]["uuid"], supplied_uuid)

    def test_returned_uuid_reflects_before_send_replacement(self):
        replacement_uuid = str(uuid.uuid4())

        def replace_uuid(event):
            event["uuid"] = replacement_uuid
            return event

        client = self._client(before_send=replace_uuid)
        with patch_capture_send("consumer") as mock_post:
            returned_uuid = client.capture_ai("$ai_generation", distinct_id="d")
            client.flush()

        self.assertEqual(returned_uuid, replacement_uuid)
        batch = sent_batch(mock_post)
        self.assertEqual(batch[0]["uuid"], replacement_uuid)

    def test_returned_uuid_is_regenerated_when_before_send_removes_it(self):
        def drop_uuid(event):
            del event["uuid"]
            return event

        client = self._client(before_send=drop_uuid)
        with patch_capture_send("consumer") as mock_post:
            returned_uuid = client.capture_ai("$ai_generation", distinct_id="d")
            client.flush()

        self.assertIsNotNone(returned_uuid)
        batch = sent_batch(mock_post)
        self.assertEqual(batch[0]["uuid"], returned_uuid)


class TestCaptureAiPrivacyMode(unittest.TestCase):
    """Privacy mode always wins over `enable_full_ai_capture`."""

    def test_privacy_mode_strips_content_despite_full_ai_capture(self):
        client = Client(
            TEST_API_KEY,
            send=False,
            enable_full_ai_capture=True,
            privacy_mode=True,
        )
        self.addCleanup(client.join)
        payload = {"role": "user", "content": "sensitive prompt"}

        sanitized = with_privacy_mode(
            client, False, finalize_ai_content(payload, ph_client=client)
        )

        self.assertIsNone(sanitized)


if __name__ == "__main__":
    unittest.main()
