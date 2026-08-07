import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any

from unittest import mock
from parameterized import parameterized

try:
    from queue import Queue
except ImportError:
    from Queue import Queue

from posthog.capture_compression import CaptureCompression
from posthog.capture_mode import CaptureMode
from posthog.consumer import MAX_MSG_SIZE, Consumer, _DrainSignal
from posthog.request import AI_EVENTS_ENDPOINT, EVENTS_ENDPOINT, APIError
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

    def test_upload(self) -> None:
        q = Queue()
        consumer = Consumer(q, TEST_API_KEY)
        q.put(_track_event())
        success = consumer.upload()
        self.assertTrue(success)

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
            line for line in logs.getvalue().splitlines() if "error uploading" in line
        ]
        expected_log = "[PostHog] error uploading: boom"
        self.assertEqual(
            [line for line in upload_logs if line == expected_log], [expected_log]
        )

    def test_flush_interval(self) -> None:
        # Put _n_ items in the queue, pausing a little bit more than
        # _flush_interval_ after each one.
        # The consumer should upload _n_ times.
        q = Queue()
        flush_interval = 0.3
        consumer = Consumer(q, TEST_API_KEY, flush_at=10, flush_interval=flush_interval)
        with mock.patch.object(consumer, "request") as mock_request:
            consumer.start()
            for i in range(3):
                q.put(_track_event("python event %d" % i))
                time.sleep(flush_interval * 1.1)
            self.assertEqual(mock_request.call_count, 3)

    def test_multiple_uploads_per_interval(self) -> None:
        # Put _flush_at*2_ items in the queue at once, then pause for
        # _flush_interval_. The consumer should upload 2 times.
        q = Queue()
        flush_interval = 0.5
        flush_at = 10
        consumer = Consumer(
            q, TEST_API_KEY, flush_at=flush_at, flush_interval=flush_interval
        )
        with mock.patch("posthog.consumer.batch_post") as mock_post:
            consumer.start()
            for i in range(flush_at * 2):
                q.put(_track_event("python event %d" % i))
            time.sleep(flush_interval * 1.1)
            self.assertEqual(mock_post.call_count, 2)

    def test_request(self) -> None:
        consumer = Consumer(None, TEST_API_KEY)
        consumer.request([_track_event()])

    def _run_retry_test(
        self, exception: Exception, exception_count: int, retries: int = 10
    ) -> None:
        call_count = [0]

        def mock_post(*args: Any, **kwargs: Any) -> None:
            call_count[0] += 1
            if call_count[0] <= exception_count:
                raise exception

        consumer = Consumer(None, TEST_API_KEY, retries=retries)
        with mock.patch(
            "posthog.consumer.batch_post", mock.Mock(side_effect=mock_post)
        ):
            if exception_count <= retries:
                consumer.request([_track_event()])
            else:
                with self.assertRaises(type(exception)):
                    consumer.request([_track_event()])

    @parameterized.expand(
        [
            ("general_errors", Exception("generic exception"), 2),
            ("server_errors", APIError(500, "Internal Server Error"), 2),
            ("rate_limit_errors", APIError(429, "Too Many Requests"), 2),
        ]
    )
    def test_request_retries_on_retriable_errors(
        self, _name: str, exception: Exception, exception_count: int
    ) -> None:
        self._run_retry_test(exception, exception_count)

    def test_request_does_not_retry_client_errors(self) -> None:
        with self.assertRaises(APIError):
            self._run_retry_test(APIError(400, "Client Errors"), 1)

    def test_request_fails_when_exceptions_exceed_retries(self) -> None:
        self._run_retry_test(APIError(500, "Internal Server Error"), 4, retries=3)

    def test_negative_retries_still_attempts_delivery_once(self) -> None:
        consumer = Consumer(None, TEST_API_KEY, retries=-1)

        with mock.patch("posthog.consumer.batch_post") as mock_post:
            consumer.request([_track_event()])

        self.assertEqual(consumer.retries, 0)
        mock_post.assert_called_once()

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

        def mock_send_fn(batch: list[dict[str, Any]], _path: str) -> None:
            request_size = len(json.dumps({"batch": batch}).encode())
            # Batches close after the first message bringing it bigger than BATCH_SIZE_LIMIT, let's add 10% of margin
            self.assertTrue(
                request_size < (5 * 1024 * 1024) * 1.1,
                "batch size (%d) higher than limit" % request_size,
            )

        with mock.patch.object(
            consumer, "_send", side_effect=mock_send_fn
        ) as mock_send:
            consumer.start()
            for _ in range(0, n_msgs + 2):
                q.put(track)
            q.join()
            self.assertEqual(mock_send.call_count, 2)

    def test_request_sleeps_with_retry_after(self) -> None:
        error = APIError(429, "Too Many Requests", retry_after=5.0)
        call_count = [0]

        def mock_post(*args: Any, **kwargs: Any) -> None:
            call_count[0] += 1
            if call_count[0] <= 1:
                raise error

        consumer = Consumer(None, TEST_API_KEY, retries=3)
        with (
            mock.patch("posthog.consumer.batch_post", side_effect=mock_post),
            mock.patch("posthog.consumer.time.sleep") as mock_sleep,
        ):
            consumer.request([_track_event()])
            mock_sleep.assert_called_once_with(5.0)

    def test_request_uses_exponential_backoff_without_retry_after(self) -> None:
        error = APIError(503, "Service Unavailable")
        call_count = [0]

        def mock_post(*args: Any, **kwargs: Any) -> None:
            call_count[0] += 1
            if call_count[0] <= 3:
                raise error

        consumer = Consumer(None, TEST_API_KEY, retries=3)
        with (
            mock.patch("posthog.consumer.batch_post", side_effect=mock_post),
            mock.patch("posthog.consumer.time.sleep") as mock_sleep,
        ):
            consumer.request([_track_event()])
            self.assertEqual(
                mock_sleep.call_args_list,
                [
                    mock.call(1),  # 2^0
                    mock.call(2),  # 2^1
                    mock.call(4),  # 2^2
                ],
            )

    @parameterized.expand(
        [
            ("huge_numeric", "1000000000", [30, 30]),
            ("small_numeric", "0.25", [1, 2]),
            ("huge_date", "Fri, 01 Jan 2100 00:00:00 GMT", [30, 30]),
            ("small_date", None, [1, 2]),
        ]
    )
    def test_request_bounds_retry_after_without_reducing_attempts(
        self, _name: str, retry_after_header: str | None, expected_sleeps: list[int]
    ) -> None:
        if retry_after_header is None:
            retry_after_header = format_datetime(
                datetime.now(timezone.utc) + timedelta(seconds=1), usegmt=True
            )

        retry_response = mock.Mock(
            status_code=503,
            headers={"Retry-After": retry_after_header},
            text="Service Unavailable",
        )
        retry_response.json.return_value = {"detail": "Service Unavailable"}
        success_response = mock.Mock(status_code=200)
        session = mock.Mock()
        session.post.side_effect = [retry_response, retry_response, success_response]

        consumer = Consumer(None, TEST_API_KEY, retries=2)
        with (
            mock.patch("posthog.request._get_session", return_value=session),
            mock.patch("posthog.consumer.time.sleep") as mock_sleep,
        ):
            consumer.request([_track_event()])

        self.assertEqual(session.post.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in mock_sleep.call_args_list], expected_sleeps
        )

    def test_request_retries_on_408(self) -> None:
        call_count = [0]

        def mock_post(*args: Any, **kwargs: Any) -> None:
            call_count[0] += 1
            if call_count[0] <= 1:
                raise APIError(408, "Request Timeout")

        consumer = Consumer(None, TEST_API_KEY, retries=3)
        with (
            mock.patch("posthog.consumer.batch_post", side_effect=mock_post),
            mock.patch("posthog.consumer.time.sleep"),
        ):
            consumer.request([_track_event()])
            self.assertEqual(call_count[0], 2)

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


class TestConsumerCaptureModeRouting(unittest.TestCase):
    """`capture_mode` selects the submitter; V0 posts to the consumer's `endpoint`."""

    @parameterized.expand(
        [
            ("v0", CaptureMode.V0, False),
            ("v1", CaptureMode.V1, True),
        ]
    )
    def test_capture_mode_selects_analytics_submitter(
        self, _name, mode, expects_v1
    ) -> None:
        consumer = Consumer(None, TEST_API_KEY, capture_mode=mode)
        batch = [_track_event()]
        with (
            mock.patch("posthog.consumer.batch_post") as mock_post,
            mock.patch("posthog.consumer._send_v1_batch") as mock_v1,
        ):
            consumer.request(batch)
        if expects_v1:
            mock_post.assert_not_called()
            mock_v1.assert_called_once()
            self.assertEqual(mock_v1.call_args.args[2], batch)
        else:
            mock_v1.assert_not_called()
            mock_post.assert_called_once()
            self.assertEqual(mock_post.call_args.kwargs["path"], EVENTS_ENDPOINT)

    def test_v1_forwards_consumer_config_to_submitter(self) -> None:
        consumer = Consumer(
            None,
            TEST_API_KEY,
            capture_mode=CaptureMode.V1,
            capture_compression=CaptureCompression.DEFLATE,
            timeout=7,
            retries=4,
            historical_migration=True,
        )
        with (
            mock.patch("posthog.consumer.batch_post"),
            mock.patch("posthog.consumer._send_v1_batch") as mock_v1,
        ):
            consumer.request([_track_event()])
            kwargs = mock_v1.call_args.kwargs
            self.assertEqual(kwargs["compression"], CaptureCompression.DEFLATE)
            self.assertEqual(kwargs["timeout"], 7)
            self.assertEqual(kwargs["max_retries"], 4)
            self.assertEqual(kwargs["historical_migration"], True)

    def test_v0_posts_to_configured_endpoint(self) -> None:
        consumer = Consumer(None, TEST_API_KEY, endpoint=AI_EVENTS_ENDPOINT)
        batch = [_ai_event()]
        with mock.patch("posthog.consumer.batch_post") as mock_post:
            consumer.request(batch)
            mock_post.assert_called_once()
            self.assertEqual(mock_post.call_args.kwargs["path"], AI_EVENTS_ENDPOINT)
            self.assertEqual(mock_post.call_args.kwargs["batch"], batch)

    def test_v1_routes_whole_batch_through_v1_submitter(self) -> None:
        # A consumer doesn't know AI events exist: with `capture_mode` v1, the
        # whole batch (including `$ai_*`-named events) rides the v1 submitter.
        consumer = Consumer(None, TEST_API_KEY, capture_mode=CaptureMode.V1)
        batch = [_ai_event(), _track_event()]
        with (
            mock.patch("posthog.consumer.batch_post") as mock_post,
            mock.patch("posthog.consumer._send_v1_batch") as mock_v1,
        ):
            consumer.request(batch)
            mock_v1.assert_called_once()
            self.assertEqual(mock_v1.call_args.args[2], batch)
            mock_post.assert_not_called()
