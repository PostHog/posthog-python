"""Regression coverage for gevent monkey-patching compatibility."""

import importlib.util
import subprocess
import sys
import textwrap
import unittest
from queue import Full, Queue
from unittest import mock

from posthog.client import Client, _new_lane_queue
from posthog.test.test_utils import FAKE_TEST_API_KEY


class TestLaneQueueFallback(unittest.TestCase):
    def test_uses_working_queue_without_importing_gevent(self):
        with mock.patch("builtins.__import__", side_effect=AssertionError):
            queue = _new_lane_queue(10)

        self.assertIsInstance(queue, Queue)

    def test_disables_capture_if_no_compatible_queue_is_available(self):
        incompatible_queue = mock.Mock(spec=[])

        with self.assertLogs("posthog", level="ERROR") as logs:
            with (
                mock.patch("posthog.client.Queue", return_value=incompatible_queue),
                mock.patch("builtins.__import__", side_effect=ImportError),
            ):
                queue = _new_lane_queue(10)

        with self.assertRaises(Full):
            queue.put("event", block=False)
        self.assertTrue(queue.empty())
        self.assertEqual(queue.unfinished_tasks, 0)
        self.assertIsNone(queue.task_done())
        self.assertIn("disabling the PostHog client", logs.output[0])

    def test_disables_client_if_no_compatible_queue_is_available(self):
        incompatible_queue = mock.Mock(spec=[])

        with self.assertLogs("posthog", level="ERROR"):
            with (
                mock.patch("posthog.client.Queue", return_value=incompatible_queue),
                mock.patch("builtins.__import__", side_effect=ImportError),
            ):
                client = Client(FAKE_TEST_API_KEY)

        self.assertTrue(client.disabled)
        self.assertEqual(client.consumers, [])
        self.assertIsNone(client.capture("disabled-queue", distinct_id="distinct_id"))
        client.flush()
        client.shutdown()


@unittest.skipUnless(importlib.util.find_spec("gevent"), "gevent is not installed")
class TestGeventCompatibility(unittest.TestCase):
    def test_capture_and_flush_after_monkey_patching(self):
        script = textwrap.dedent(
            """
            import gevent.monkey

            gevent.monkey.patch_all()

            import queue

            assert gevent.monkey.is_object_patched("queue", "Queue"), (
                "gevent did not replace queue.Queue; the regression scenario "
                "is not being exercised"
            )

            from unittest import mock

            with mock.patch("posthog.consumer.batch_post") as mock_post:
                from posthog.client import Client

                client = Client("phc_test", flush_at=1, flush_interval=60)
                original_queue = gevent.monkey.get_original("queue", "Queue")
                assert isinstance(client.queue, original_queue)
                assert not isinstance(client.queue, queue.Queue)
                for attribute in (
                    "mutex",
                    "not_empty",
                    "not_full",
                    "all_tasks_done",
                    "unfinished_tasks",
                    "_qsize",
                    "_get",
                ):
                    assert hasattr(client.queue, attribute), attribute

                client.capture("gevent-regression", distinct_id="distinct_id")
                client.flush(timeout_seconds=10)
                client.join()

            assert mock_post.called, "batch_post was never called"
            assert client.queue.empty(), "flush did not drain the queue"
            """
        )

        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
