import asyncio
import threading
import time
import unittest
from unittest import mock

from posthog._async_utils import _BackgroundEventLoopRunner


class TestBackgroundEventLoopRunner(unittest.TestCase):
    def test_startup_error_is_reported(self):
        runner = _BackgroundEventLoopRunner()
        awaitable = asyncio.sleep(0)

        with mock.patch(
            "posthog._async_utils.asyncio.new_event_loop",
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

        original_new_event_loop = asyncio.new_event_loop

        def create_loop():
            construction_started.set()
            self.assertTrue(release_construction.wait(2))
            return original_new_event_loop()

        def run():
            awaitable = asyncio.sleep(0)
            try:
                runner.run(awaitable)
            except BaseException as error:
                awaitable.close()
                run_errors.append(error)

        with mock.patch(
            "posthog._async_utils.asyncio.new_event_loop", side_effect=create_loop
        ):
            run_thread = threading.Thread(target=run)
            run_thread.start()
            self.assertTrue(construction_started.wait(1))

            close_thread = threading.Thread(target=runner.close)
            close_thread.start()
            deadline = time.monotonic() + 1
            while not runner._close_requested:
                if time.monotonic() >= deadline:
                    self.fail("close did not reach startup state")
                time.sleep(0.001)
            release_construction.set()
            run_thread.join(2)
            close_thread.join(2)

        self.assertFalse(run_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertEqual(len(run_errors), 1)
        self.assertRegex(str(run_errors[0]), "closed during startup")
        self.assertIsNone(runner._thread)
        self.assertIsNone(runner._loop)

    def test_runner_preserves_configured_event_loop_policy(self):
        runner = _BackgroundEventLoopRunner()
        original_policy = asyncio.get_event_loop_policy()

        class Policy(asyncio.DefaultEventLoopPolicy):
            loop = None

            def new_event_loop(self):
                self.loop = super().new_event_loop()
                return self.loop

        policy = Policy()

        async def running_loop():
            return asyncio.get_running_loop()

        try:
            asyncio.set_event_loop_policy(policy)
            self.assertIs(runner.run(running_loop()), policy.loop)
            runner.close()
        finally:
            asyncio.set_event_loop_policy(original_policy)

    def test_run_from_runner_thread_fails_instead_of_deadlocking(self):
        runner = _BackgroundEventLoopRunner()

        async def reenter():
            awaitable = asyncio.sleep(0)
            try:
                with self.assertRaisesRegex(RuntimeError, "runner thread"):
                    runner.run(awaitable)
            finally:
                awaitable.close()

        runner.run(reenter())
        runner.close()

    def test_close_from_runner_thread_allows_fresh_loop(self):
        runner = _BackgroundEventLoopRunner()

        async def close_runner():
            runner.close()

        runner.run(close_runner())
        runner.run(asyncio.sleep(0))
        runner.close()
