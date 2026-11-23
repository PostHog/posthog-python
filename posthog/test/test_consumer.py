import json
import threading
import time
import unittest

from unittest import mock
from parameterized import parameterized

try:
    from queue import Queue
except ImportError:
    from Queue import Queue

from posthog.capture_compression import CaptureCompression
from posthog.capture_send import _CAPTURE_AI_V1_PATH, _CAPTURE_V1_PATH
from posthog.consumer import MAX_MSG_SIZE, Consumer, _DrainSignal
from posthog.test.capture_helpers import patch_capture_send, sent_batch
from posthog.test.logging_helpers import capture_message_only_logs
from posthog.test.test_utils import TEST_API_KEY


def _track_event(event_name: str = "python event") -> dict[str, str]:
    return {"type": "track", "event": event_name, "distinct_id": "distinct_id"}


class TestConsumer(unittest.TestCase):
    def test_next(self) -> None:
        q = Queue()
        consumer = Consumer(q, "")
        q.put(1)
        next = consumer.next()
        self.assertEqual(next, [1])

    def test_next_does_not_take_queued_items_after_non_draining_pause(self) -> None:
        q = Queue()
        consumer = Consumer(q, "", flush_at=100)
        drain_signal = _DrainSignal(q)
        consumer._set_drain_signal(drain_signal)
        for item in range(10):
            q.put(item)

        consumer.pause()

        self.assertEqual(consumer.next(), [])
        self.assertEqual(q.qsize(), 10)
        self.assertEqual(q.unfinished_tasks, 10)

    def test_non_draining_pause_overrides_active_flush_signal(self) -> None:
        q = Queue()
        consumer = Consumer(q, "", flush_at=100)
        drain_signal = _DrainSignal(q)
        consumer._set_drain_signal(drain_signal)
        q.put(_track_event())

        drain_signal.request()
        consumer.pause()
        try:
            self.assertEqual(consumer.next(), [])
            self.assertEqual(q.qsize(), 1)
            self.assertEqual(q.unfinished_tasks, 1)
        finally:
            drain_signal.complete()

    def test_non_draining_pause_between_drain_snapshot_and_dequeue(self) -> None:
        q = Queue()
        consumer = Consumer(q, "", flush_at=100)
        drain_signal = _DrainSignal(q)
        consumer._set_drain_signal(drain_signal)
        q.put(_track_event())
        drain_signal.request()
        original_get = drain_signal.get

        def pause_then_get(*args, **kwargs):
            consumer.pause()
            return original_get(*args, **kwargs)

        with mock.patch.object(drain_signal, "get", side_effect=pause_then_get):
            self.assertEqual(consumer.next(), [])

        drain_signal.complete()
        self.assertEqual(q.qsize(), 1)
        self.assertEqual(q.unfinished_tasks, 1)

    def test_pause_publishes_stop_under_queue_dequeue_lock(self) -> None:
        q = Queue()
        consumer = Consumer(q, "")
        drain_signal = _DrainSignal(q)
        stop_started = threading.Event()
        original_stop = drain_signal.stop

        def observed_stop(target, drain):
            stop_started.set()
            original_stop(target, drain)

        drain_signal.stop = observed_stop  # type: ignore[method-assign]
        consumer._set_drain_signal(drain_signal)

        with q.mutex:
            pause_thread = threading.Thread(target=consumer.pause)
            pause_thread.start()
            self.assertTrue(stop_started.wait(1))
            self.assertTrue(consumer.running)

        pause_thread.join(1)
        self.assertFalse(pause_thread.is_alive())
        self.assertFalse(consumer.running)

    def test_non_draining_pause_discards_buffered_partial_batch(self) -> None:
        q = Queue()
        consumer = Consumer(q, "", flush_at=100, flush_interval=60)
        consumer._set_drain_signal(_DrainSignal(q))
        request_called = threading.Event()
        consumer.request = lambda batch: request_called.set()  # type: ignore[method-assign]
        consumer.start()
        q.put(_track_event())

        deadline = time.monotonic() + 1
        while not q.empty():
            if time.monotonic() >= deadline:
                self.fail("consumer did not buffer the queued event")
            time.sleep(0.001)

        consumer.pause()
        consumer.join(1)

        self.assertFalse(consumer.is_alive())
        self.assertFalse(request_called.is_set())
        self.assertEqual(q.unfinished_tasks, 0)

    def test_pause_does_not_wait_for_active_request(self) -> None:
        q = Queue()
        consumer = Consumer(q, "", flush_at=1)
        consumer._set_drain_signal(_DrainSignal(q))
        request_started = threading.Event()
        release_request = threading.Event()

        def request(batch):
            request_started.set()
            self.assertTrue(release_request.wait(2))

        consumer.request = request  # type: ignore[method-assign]
        consumer.start()
        q.put(_track_event())
        self.assertTrue(request_started.wait(1))

        consumer.pause()

        self.assertFalse(consumer.running)
        self.assertTrue(consumer.is_alive())
        release_request.set()
        consumer.join(1)
        self.assertFalse(consumer.is_alive())

    def test_next_still_takes_queued_items_when_paused_for_drain(self) -> None:
        q = Queue()
        consumer = Consumer(q, "", flush_at=100)
        drain_signal = _DrainSignal(q)
        consumer._set_drain_signal(drain_signal)
        for item in range(10):
            q.put(item)

        drain_signal.request()
        consumer._pause(drain=True)
        try:
            self.assertEqual(consumer.next(), list(range(10)))
        finally:
            drain_signal.complete()

    def test_next_limit(self) -> None:
        q = Queue()
        flush_at = 50
        consumer = Consumer(q, "", flush_at)
        for i in range(10000):
            q.put(i)
        next = consumer.next()
        self.assertEqual(next, list(range(flush_at)))

    def test_dropping_oversize_msg(self) -> None:
        q = Queue()
        consumer = Consumer(q, "")
        oversize_msg = {"m": "x" * MAX_MSG_SIZE}
        q.put(oversize_msg)
        next = consumer.next()
        self.assertEqual(next, [])
        self.assertTrue(q.empty())
        self.assertEqual(q.unfinished_tasks, 0)

    def test_next_balances_dequeued_work_if_batching_is_interrupted(self) -> None:
        q = Queue()
        consumer = Consumer(q, "")
        q.put(_track_event())

        with mock.patch("posthog.consumer.json.dumps", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                consumer.next()

        self.assertTrue(q.empty())
        self.assertEqual(q.unfinished_tasks, 0)

    def test_max_msg_size_param_raises_per_event_ceiling(self) -> None:
        q = Queue()
        consumer = Consumer(q, "", flush_at=1, max_msg_size=4 * MAX_MSG_SIZE)
        big_msg = {"m": "x" * (2 * MAX_MSG_SIZE)}
        q.put(big_msg)
        self.assertEqual(consumer.next(), [big_msg])

    @parameterized.expand(
        [
            # A small event serializes to 18 bytes, the "big" one to 81.
            ("closes_before_overflow", 43, [0, 1, 2, 3], [[0, 1], [2, 3]]),
            ("event_over_limit_goes_alone", 30, [0, "big", 1], [[0], ["big"], [1]]),
        ]
    )
    def test_batch_byte_limit_is_checked_before_appending(
        self, _name, limit, ids, expected
    ) -> None:
        q = Queue()
        consumer = Consumer(q, "", flush_at=10, flush_interval=0.01)
        for i in ids:
            q.put({"m": "x" * (60 if i == "big" else 1), "i": i})

        with mock.patch("posthog.consumer.BATCH_SIZE_LIMIT", limit):
            batches = [[e["i"] for e in consumer.next()] for _ in expected]

        self.assertEqual(batches, expected)
        self.assertEqual(q.unfinished_tasks, len(ids))

    def test_upload(self) -> None:
        q = Queue()
        consumer = Consumer(q, TEST_API_KEY, flush_at=1)
        event = _track_event()
        q.put(event)
        with patch_capture_send("consumer") as post:
            success = consumer.upload()
        self.assertTrue(success)
        post.assert_called_once()
        self.assertEqual(sent_batch(post), [event])
        self.assertEqual(q.unfinished_tasks, 0)
        self.assertTrue(q.empty())

    def test_message_only_error_logs_include_posthog_prefix(self) -> None:
        q = Queue()
        consumer = Consumer(q, TEST_API_KEY)
        q.put(_track_event())

        with mock.patch.object(consumer, "request", side_effect=Exception("boom")):
            with capture_message_only_logs() as logs:
                success = consumer.upload()

        self.assertFalse(success)
        # `capture_message_only_logs` taps the process-wide "posthog" logger and
        # `upload()` spans a whole flush_interval, so background threads left by
        # other tests can log into the same stream. Assert on the line under
        # test rather than on the entire capture.
        upload_logs = [
            line for line in logs.getvalue().splitlines() if "not persisted" in line
        ]
        expected_log = (
            "[PostHog] 1 event(s) not persisted by /i/v1/analytics/events: Exception"
        )
        self.assertEqual(
            [line for line in upload_logs if line == expected_log], [expected_log]
        )

    def test_flush_interval(self) -> None:
        q = Queue()
        flush_interval = 0.3
        consumer = Consumer(q, TEST_API_KEY, flush_at=10, flush_interval=flush_interval)
        delivered = threading.Event()
        with mock.patch.object(
            consumer, "request", side_effect=lambda batch: delivered.set()
        ) as mock_request:
            consumer.start()
            try:
                for i in range(3):
                    delivered.clear()
                    event = _track_event("python event %d" % i)
                    q.put(event)
                    self.assertTrue(delivered.wait(5))
                    self.assertEqual(mock_request.call_args.args[0], [event])
                self.assertEqual(mock_request.call_count, 3)
            finally:
                consumer.pause()
                consumer.join(5)
            self.assertFalse(consumer.is_alive())

    def test_partial_batch_waits_for_remaining_flush_interval(self) -> None:
        from queue import Empty

        q = Queue()
        event = _track_event()
        q.put(event)
        consumer = Consumer(q, TEST_API_KEY, flush_at=10, flush_interval=0.3)
        now = [100.0]
        waits = []
        original_get = q.get

        def timed_get(*, block, timeout):
            waits.append(timeout)
            if len(waits) == 1:
                now[0] += 0.125
                return original_get(block=False)
            now[0] += timeout
            raise Empty

        with (
            mock.patch("posthog.consumer.time.monotonic", side_effect=lambda: now[0]),
            mock.patch.object(q, "get", side_effect=timed_get),
            mock.patch.object(consumer, "request") as request,
        ):
            self.assertTrue(consumer.upload())

        self.assertEqual(len(waits), 2)
        self.assertAlmostEqual(waits[0], 0.3)
        self.assertAlmostEqual(waits[1], 0.175)
        self.assertAlmostEqual(now[0], 100.3)
        request.assert_called_once_with([event])
        self.assertEqual(q.unfinished_tasks, 0)

    def test_multiple_uploads_per_interval(self) -> None:
        q = Queue()
        flush_interval = 10
        flush_at = 10
        consumer = Consumer(
            q, TEST_API_KEY, flush_at=flush_at, flush_interval=flush_interval
        )
        delivered = threading.Event()
        batches = []

        def record_batch(api_key, host, batch, **kwargs):
            batches.append(batch)
            if len(batches) == 2:
                delivered.set()

        with patch_capture_send("consumer", side_effect=record_batch):
            consumer.start()
            try:
                events = [
                    _track_event("python event %d" % i) for i in range(flush_at * 2)
                ]
                for event in events:
                    q.put(event)
                self.assertTrue(delivered.wait(5))
                self.assertEqual(batches, [events[:10], events[10:]])
            finally:
                consumer.pause()
                consumer.join(15)
            self.assertFalse(consumer.is_alive())

    def test_negative_retries_still_attempts_delivery_once(self) -> None:
        consumer = Consumer(None, TEST_API_KEY, retries=-1)

        with patch_capture_send("consumer") as mock_send:
            consumer.request([_track_event()])

        self.assertEqual(consumer.retries, 0)
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args.kwargs["max_retries"], 0)

    def test_pause(self) -> None:
        consumer = Consumer(None, TEST_API_KEY)
        consumer.pause()
        self.assertFalse(consumer.running)

    def test_drain_signal_returns_partial_batch_without_waiting(self) -> None:
        # A drain request means "send what is queued now", so `next()` must not
        # hold a below-flush_at batch back for the rest of flush_interval.
        q = Queue()
        signal = _DrainSignal(q)
        consumer = Consumer(q, TEST_API_KEY, flush_at=100, flush_interval=30)
        consumer._set_drain_signal(signal)
        q.put(_track_event("first"))
        q.put(_track_event("second"))
        signal.request()

        start = time.monotonic()
        batch = consumer.next()
        signal.complete()

        self.assertEqual(len(batch), 2)
        self.assertLess(time.monotonic() - start, 5)

    def test_drain_signal_still_respects_flush_at(self) -> None:
        # Draining must not degrade batching into one request per event.
        q = Queue()
        signal = _DrainSignal(q)
        flush_at = 10
        consumer = Consumer(q, TEST_API_KEY, flush_at=flush_at, flush_interval=30)
        consumer._set_drain_signal(signal)
        for i in range(flush_at * 3):
            q.put(_track_event("python event %d" % i))
        signal.request()

        self.assertEqual(len(consumer.next()), flush_at)
        signal.complete()

    def test_completed_drain_request_restores_normal_batching(self) -> None:
        # Once the caller completes its request, later batches must go back to
        # normal timer-based batching instead of inheriting a stale drain.
        q = Queue()
        signal = _DrainSignal(q)
        flush_interval = 0.2
        consumer = Consumer(
            q,
            TEST_API_KEY,
            flush_at=100,
            flush_interval=flush_interval,
        )
        consumer._set_drain_signal(signal)
        q.put(_track_event())
        signal.request()
        self.assertEqual(len(consumer.next()), 1)
        signal.complete()

        start = time.monotonic()
        self.assertEqual(consumer.next(), [])
        self.assertGreaterEqual(time.monotonic() - start, flush_interval * 0.5)

    def test_overlapping_drain_requests_remain_active_until_all_complete(self) -> None:
        q = Queue()
        signal = _DrainSignal(q)

        signal.request()
        signal.request()
        signal.complete()
        self.assertTrue(signal.requested)

        signal.complete()
        self.assertFalse(signal.requested)

    def test_consecutive_drain_requests_each_drain_immediately(self) -> None:
        # A later flush must not be served by an earlier flush's bookkeeping.
        q = Queue()
        signal = _DrainSignal(q)
        consumer = Consumer(q, TEST_API_KEY, flush_at=100, flush_interval=30)
        consumer._set_drain_signal(signal)

        for i in range(3):
            q.put(_track_event("python event %d" % i))
            signal.request()
            start = time.monotonic()
            self.assertEqual(len(consumer.next()), 1)
            signal.complete()
            self.assertLess(time.monotonic() - start, 5)

    def test_drain_signal_wakes_a_consumer_mid_batch(self) -> None:
        # The realistic ordering: the consumer is already parked on a partial
        # batch when flush() signals it.
        q = Queue()
        signal = _DrainSignal(q)
        consumer = Consumer(q, TEST_API_KEY, flush_at=100, flush_interval=30)
        consumer._set_drain_signal(signal)
        q.put(_track_event())
        threading.Timer(0.1, signal.request).start()

        start = time.monotonic()
        batch = consumer.next()
        signal.complete()

        self.assertEqual(len(batch), 1)
        self.assertLess(time.monotonic() - start, 5)

    def test_drain_signal_wakes_an_idle_consumer(self) -> None:
        q = Queue()
        signal = _DrainSignal(q)
        consumer = Consumer(q, TEST_API_KEY, flush_at=100, flush_interval=2)
        consumer._set_drain_signal(signal)
        threading.Timer(0.1, signal.request).start()

        start = time.monotonic()
        batch = consumer.next()
        signal.complete()

        self.assertEqual(batch, [])
        self.assertLess(time.monotonic() - start, 1)

    def test_idle_consumer_parks_while_drain_waits_for_an_upload(self) -> None:
        q = Queue()
        signal = _DrainSignal(q)
        upload_started = threading.Event()
        release_upload = threading.Event()
        idle_returned = threading.Event()
        idle_next_calls = 0

        uploading = Consumer(q, TEST_API_KEY, flush_at=1, flush_interval=30)
        idle = Consumer(q, TEST_API_KEY, flush_at=100, flush_interval=30)
        uploading._set_drain_signal(signal)
        idle._set_drain_signal(signal)

        def blocking_request(batch) -> None:
            upload_started.set()
            self.assertTrue(release_upload.wait(2))

        original_idle_next = idle.next

        def counted_idle_next():
            nonlocal idle_next_calls
            batch = original_idle_next()
            idle_next_calls += 1
            idle_returned.set()
            return batch

        uploading.request = blocking_request
        idle.next = counted_idle_next
        q.put(_track_event())
        uploading.start()
        self.assertTrue(upload_started.wait(1))
        idle.start()
        signal.request()

        try:
            self.assertTrue(idle_returned.wait(1))
            time.sleep(0.1)
            self.assertEqual(idle_next_calls, 1)
        finally:
            uploading.pause()
            idle.pause()
            release_upload.set()
            signal.complete()
            uploading.join(2)
            idle.join(2)

        self.assertFalse(uploading.is_alive())
        self.assertFalse(idle.is_alive())

    def test_without_drain_signal_batching_is_unchanged(self) -> None:
        q = Queue()
        flush_interval = 0.3
        consumer = Consumer(
            q, TEST_API_KEY, flush_at=100, flush_interval=flush_interval
        )
        q.put(_track_event())

        start = time.monotonic()
        self.assertEqual(len(consumer.next()), 1)
        self.assertGreaterEqual(time.monotonic() - start, flush_interval * 0.5)

    def test_max_batch_size(self) -> None:
        q = Queue()
        consumer = Consumer(q, TEST_API_KEY, flush_at=100000, flush_interval=3)
        properties = {}
        for n in range(0, 500):
            properties[str(n)] = "one_long_property_value_to_build_a_big_event"
        track = {
            "type": "track",
            "event": "python event",
            "distinct_id": "distinct_id",
            "properties": properties,
        }
        msg_size = len(json.dumps(track).encode())
        # Let's capture 8MB of data to trigger two batches
        n_msgs = int(8_000_000 / msg_size)

        with mock.patch.object(consumer, "request") as mock_send:
            consumer.start()
            try:
                for _ in range(0, n_msgs + 2):
                    q.put(track)
                q.join()
                self.assertEqual(mock_send.call_count, 2)
                batches = [call.args[0] for call in mock_send.call_args_list]
                self.assertEqual(sum(map(len, batches)), n_msgs + 2)
                for batch in batches:
                    request_size = len(json.dumps({"batch": batch}).encode())
                    # The event crossing the byte limit is included in the batch.
                    self.assertLess(request_size, (5 * 1024 * 1024) * 1.1)
            finally:
                consumer.pause()
                consumer.join(5)
            self.assertFalse(consumer.is_alive())

    @parameterized.expand(
        [
            ("on_error_succeeds", False),
            ("on_error_raises", True),
        ]
    )
    def test_upload_exception_calls_on_error_and_does_not_raise(
        self, _name: str, on_error_raises: bool
    ) -> None:
        on_error_called: list[tuple[Exception, list[dict[str, str]]]] = []

        def on_error(e: Exception, batch: list[dict[str, str]]) -> None:
            on_error_called.append((e, batch))
            if on_error_raises:
                raise Exception("on_error failed")

        q = Queue()
        consumer = Consumer(q, TEST_API_KEY, on_error=on_error)
        track = _track_event()
        q.put(track)

        with mock.patch.object(
            consumer, "request", side_effect=Exception("request failed")
        ):
            result = consumer.upload()

        self.assertFalse(result)
        self.assertEqual(len(on_error_called), 1)
        self.assertEqual(str(on_error_called[0][0]), "request failed")
        self.assertEqual(on_error_called[0][1], [track])


def _ai_event(event_name: str = "$ai_generation") -> dict[str, str]:
    return {"type": "track", "event": event_name, "distinct_id": "distinct_id"}


class TestConsumerSubmitterRouting(unittest.TestCase):
    """Every consumer sends through the capture v1 submitter to its `endpoint`."""

    def test_default_posts_to_analytics_endpoint(self) -> None:
        consumer = Consumer(None, TEST_API_KEY)
        batch = [_track_event()]
        with patch_capture_send("consumer") as mock_v1:
            consumer.request(batch)
        mock_v1.assert_called_once()
        self.assertEqual(sent_batch(mock_v1), batch)
        self.assertEqual(mock_v1.call_args.kwargs["path"], _CAPTURE_V1_PATH)

    def test_forwards_consumer_config_to_submitter(self) -> None:
        consumer = Consumer(
            None,
            TEST_API_KEY,
            capture_compression=CaptureCompression.DEFLATE,
            timeout=7,
            retries=4,
            historical_migration=True,
        )
        with patch_capture_send("consumer") as mock_v1:
            consumer.request([_track_event()])
            kwargs = mock_v1.call_args.kwargs
            self.assertEqual(kwargs["compression"], CaptureCompression.DEFLATE)
            self.assertEqual(kwargs["timeout"], 7)
            self.assertEqual(kwargs["max_retries"], 4)
            self.assertEqual(kwargs["historical_migration"], True)

    def test_posts_to_configured_endpoint(self) -> None:
        consumer = Consumer(None, TEST_API_KEY, endpoint=_CAPTURE_AI_V1_PATH)
        batch = [_ai_event()]
        with patch_capture_send("consumer") as mock_v1:
            consumer.request(batch)
        mock_v1.assert_called_once()
        self.assertEqual(mock_v1.call_args.kwargs["path"], _CAPTURE_AI_V1_PATH)
        self.assertEqual(sent_batch(mock_v1), batch)
