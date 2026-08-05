import asyncio
import threading
import unittest
from unittest import mock

from posthog._async_utils import _BackgroundEventLoopRunner, _ContextEventLoop


class TestBackgroundEventLoopRunner(unittest.TestCase):
    def test_startup_error_is_reported(self):
        runner = _BackgroundEventLoopRunner()
        awaitable = asyncio.sleep(0)

        with mock.patch(
            "posthog._async_utils._ContextEventLoop",
            side_effect=RuntimeError("startup failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                runner.run(awaitable)

        awaitable.close()

    def test_close_during_startup_does_not_orphan_thread(self):
        runner = _BackgroundEventLoopRunner()
        construction_started = threading.Event()
        release_construction = threading.Event()

        def create_loop():
            construction_started.set()
            self.assertTrue(release_construction.wait(2))
            return _ContextEventLoop()

        with mock.patch(
            "posthog._async_utils._ContextEventLoop", side_effect=create_loop
        ):
            ensure_thread = threading.Thread(target=runner._ensure_loop)
            ensure_thread.start()
            self.assertTrue(construction_started.wait(1))

            close_thread = threading.Thread(target=runner.close)
            close_thread.start()
            release_construction.set()
            ensure_thread.join(2)
            close_thread.join(2)

        self.assertFalse(ensure_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertIsNone(runner._thread)
        self.assertIsNone(runner._loop)
