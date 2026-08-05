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

    def test_close_waits_for_run_during_startup(self):
        runner = _BackgroundEventLoopRunner()
        construction_started = threading.Event()
        release_construction = threading.Event()
        run_errors = []

        def create_loop():
            construction_started.set()
            self.assertTrue(release_construction.wait(2))
            return _ContextEventLoop()

        def run():
            try:
                runner.run(asyncio.sleep(0))
            except BaseException as error:
                run_errors.append(error)

        with mock.patch(
            "posthog._async_utils._ContextEventLoop", side_effect=create_loop
        ):
            run_thread = threading.Thread(target=run)
            run_thread.start()
            self.assertTrue(construction_started.wait(1))

            close_thread = threading.Thread(target=runner.close)
            close_thread.start()
            release_construction.set()
            run_thread.join(2)
            close_thread.join(2)

        self.assertFalse(run_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertEqual(run_errors, [])
        self.assertIsNone(runner._thread)
        self.assertIsNone(runner._loop)
