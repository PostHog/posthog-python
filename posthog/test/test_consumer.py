import json
import time
import unittest
from typing import Any

import mock
from parameterized import parameterized

try:
    from queue import Queue
except ImportError:
    from Queue import Queue

from posthog.consumer import MAX_MSG_SIZE, Consumer
from posthog.request import APIError
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

    def test_upload(self) -> None:
        q = Queue()
        consumer = Consumer(q, TEST_API_KEY)
        q.put(_track_event())
        success = consumer.upload()
        self.assertTrue(success)

    def test_flush_interval(self) -> None:
        # Put _n_ items in the queue, pausing a little bit more than
        # _flush_interval_ after each one.
        # The consumer should upload _n_ times.
        q = Queue()
        flush_interval = 0.3
        consumer = Consumer(q, TEST_API_KEY, flush_at=10, flush_interval=flush_interval)
        with mock.patch("posthog.consumer.batch_post") as mock_post:
            consumer.start()
            for i in range(3):
                q.put(_track_event("python event %d" % i))
                time.sleep(flush_interval * 1.1)
            self.assertEqual(mock_post.call_count, 3)

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

    def test_pause(self) -> None:
        consumer = Consumer(None, TEST_API_KEY)
        consumer.pause()
        self.assertFalse(consumer.running)

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

        def mock_post_fn(_: str, data: str, **kwargs: Any) -> mock.Mock:
            res = mock.Mock()
            res.status_code = 200
            request_size = len(data.encode())
            # Batches close after the first message bringing it bigger than BATCH_SIZE_LIMIT, let's add 10% of margin
            self.assertTrue(
                request_size < (5 * 1024 * 1024) * 1.1,
                "batch size (%d) higher than limit" % request_size,
            )
            return res

        with mock.patch(
            "posthog.request._session.post", side_effect=mock_post_fn
        ) as mock_post:
            consumer.start()
            for _ in range(0, n_msgs + 2):
                q.put(track)
            q.join()
            self.assertEqual(mock_post.call_count, 2)

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
