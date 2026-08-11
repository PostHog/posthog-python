"""Regression tests for #865: event delivery must survive gevent monkey-patching.

gevent's ``monkey.patch_all()`` replaces ``queue.Queue`` with an implementation
that lacks CPython's private synchronization attributes (``mutex``,
``not_empty``, ``not_full``, ``all_tasks_done``, ``unfinished_tasks``,
``_qsize``, ``_get``). The consumer and flush paths synchronize on those
attributes, so a client whose lane queue is a gevent queue delivers nothing:
the consumer thread dies on ``queue.not_empty`` and ``flush()`` raises on
``queue.all_tasks_done``. The fix gives lanes an SDK-owned queue
(``posthog._queue.LaneQueue``) that no runtime can swap out.
"""

import importlib.util
import subprocess
import sys
import textwrap
import unittest
from queue import Empty, Full
from unittest import mock

from posthog._queue import LaneQueue
from posthog.client import Client
from posthog.test.test_utils import FAKE_TEST_API_KEY


class TestLaneQueue(unittest.TestCase):
    def test_exposes_the_private_synchronization_interface(self):
        # Every private attribute the consumer and flush paths touch. A missing
        # one is exactly the gevent failure mode, so this list is the contract.
        queue = LaneQueue(10)
        for attribute in (
            "mutex",
            "not_empty",
            "not_full",
            "all_tasks_done",
            "unfinished_tasks",
            "_qsize",
            "_get",
        ):
            self.assertTrue(hasattr(queue, attribute), attribute)

    def test_round_trip_and_task_accounting(self):
        queue = LaneQueue(2)
        queue.put("a")
        queue.put("b")
        self.assertEqual(queue.qsize(), 2)
        self.assertTrue(queue.full())
        with self.assertRaises(Full):
            queue.put("c", block=False)

        self.assertEqual(queue.get_nowait(), "a")
        self.assertEqual(queue.get_nowait(), "b")
        with self.assertRaises(Empty):
            queue.get_nowait()

        self.assertEqual(queue.unfinished_tasks, 2)
        queue.task_done()
        queue.task_done()
        self.assertEqual(queue.unfinished_tasks, 0)
        queue.join()
        with self.assertRaises(ValueError):
            queue.task_done()

    def test_get_timeout_raises_empty(self):
        queue = LaneQueue(1)
        with self.assertRaises(Empty):
            queue.get(timeout=0.01)


class TestLaneQueueIsSdkOwned(unittest.TestCase):
    def test_capture_and_flush_use_the_sdk_queue(self):
        with mock.patch("posthog.consumer.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, flush_at=1, flush_interval=60)
            self.assertIsInstance(client.queue, LaneQueue)
            client.capture("gevent-regression", distinct_id="distinct_id")
            client.flush(timeout_seconds=10)
        self.assertTrue(mock_post.called)
        self.assertTrue(client.queue.empty())


@unittest.skipUnless(importlib.util.find_spec("gevent"), "gevent is not installed")
class TestUnderGeventMonkeyPatching(unittest.TestCase):
    def test_capture_and_flush_deliver_events(self):
        # Run in a subprocess: monkey-patching is process-wide and permanent,
        # so it cannot run inside the test process. The script mirrors a
        # gunicorn gevent worker: patch first, import the SDK after.
        script = textwrap.dedent(
            """
            import gevent.monkey

            gevent.monkey.patch_all()

            from unittest import mock

            with mock.patch("posthog.consumer.batch_post") as mock_post:
                from posthog.client import Client

                client = Client("fake_key", flush_at=1, flush_interval=60)
                client.capture("gevent-regression", distinct_id="distinct_id")
                client.flush(timeout_seconds=10)

            assert mock_post.called, "batch_post was never called under gevent"
            assert client.queue.empty(), "flush() did not drain the queue"
            print("OK")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)
