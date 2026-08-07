import asyncio
import threading
import time
import unittest
from unittest import mock

from posthog._async_utils import _BackgroundEventLoopRunner, _LoopStartup


class _PausingEvent:
    def __init__(self) -> None:
        self._completed = threading.Event()
        self.waiter_paused = threading.Event()
        self.release_waiter = threading.Event()

    def set(self) -> None:
        self._completed.set()

    def wait(self, timeout=None) -> bool:
        if not self._completed.wait(timeout):
            return False
        self.waiter_paused.set()
        return self.release_waiter.wait(2)


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

        self.assertIsNone(awaitable.cr_frame)

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

    def test_run_retries_when_close_wins_after_startup_completes(self):
        runner = _BackgroundEventLoopRunner()
        first_startup = _LoopStartup()
        first_startup.done = _PausingEvent()  # type: ignore[assignment]
        second_startup = _LoopStartup()
        run_results = []
        run_errors = []

        async def result():
            return 42

        def run():
            try:
                run_results.append(runner.run(result()))
            except BaseException as error:
                run_errors.append(error)

        with mock.patch(
            "posthog._async_utils._LoopStartup",
            side_effect=[first_startup, second_startup],
        ):
            run_thread = threading.Thread(target=run)
            run_thread.start()
            self.assertTrue(first_startup.done.waiter_paused.wait(1))

            runner.close()
            first_startup.done.release_waiter.set()
            run_thread.join(2)

        runner.close()
        self.assertFalse(run_thread.is_alive())
        self.assertEqual(run_errors, [])
        self.assertEqual(run_results, [42])

    def test_startup_failure_state_is_not_overwritten_by_next_attempt(self):
        runner = _BackgroundEventLoopRunner()
        first_startup = _LoopStartup()
        first_startup.done = _PausingEvent()  # type: ignore[assignment]
        second_startup = _LoopStartup()
        original_new_event_loop = asyncio.new_event_loop
        loop_attempt = 0
        first_errors = []
        second_results = []

        def create_loop():
            nonlocal loop_attempt
            loop_attempt += 1
            if loop_attempt == 1:
                raise RuntimeError("first startup failed")
            return original_new_event_loop()

        async def result():
            return 42

        first_awaitable = asyncio.sleep(0)

        def first_run():
            try:
                runner.run(first_awaitable)
            except BaseException as error:
                first_errors.append(error)

        def second_run():
            second_results.append(runner.run(result()))

        with (
            mock.patch(
                "posthog._async_utils._LoopStartup",
                side_effect=[first_startup, second_startup],
            ),
            mock.patch(
                "posthog._async_utils.asyncio.new_event_loop",
                side_effect=create_loop,
            ),
        ):
            first_thread = threading.Thread(target=first_run)
            first_thread.start()
            self.assertTrue(first_startup.done.waiter_paused.wait(1))

            second_thread = threading.Thread(target=second_run)
            second_thread.start()
            second_thread.join(2)

            first_startup.done.release_waiter.set()
            first_thread.join(2)

        runner.close()
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(len(first_errors), 1)
        self.assertRegex(str(first_errors[0]), "first startup failed")
        self.assertEqual(second_results, [42])
        self.assertIsNone(first_awaitable.cr_frame)

    def test_close_closes_awaitable_cancelled_before_first_step(self):
        runner = _BackgroundEventLoopRunner()
        runner.run(asyncio.sleep(0))
        loop = runner._loop
        self.assertIsNotNone(loop)

        loop_blocked = threading.Event()
        release_loop = threading.Event()
        scheduled = threading.Event()
        run_errors = []
        original_run_coroutine_threadsafe = asyncio.run_coroutine_threadsafe

        def block_loop():
            loop_blocked.set()
            self.assertTrue(release_loop.wait(2))

        def schedule(coro, target_loop):
            future = original_run_coroutine_threadsafe(coro, target_loop)
            scheduled.set()
            return future

        loop.call_soon_threadsafe(block_loop)  # type: ignore[union-attr]
        self.assertTrue(loop_blocked.wait(1))
        awaitable = asyncio.sleep(0)

        def run():
            try:
                runner.run(awaitable)
            except BaseException as error:
                run_errors.append(error)

        with mock.patch(
            "posthog._async_utils.asyncio.run_coroutine_threadsafe",
            side_effect=schedule,
        ):
            run_thread = threading.Thread(target=run)
            run_thread.start()
            self.assertTrue(scheduled.wait(1))

            close_thread = threading.Thread(target=runner.close)
            close_thread.start()
            release_loop.set()
            run_thread.join(2)
            close_thread.join(2)

        self.assertFalse(run_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertEqual(len(run_errors), 1)
        self.assertIsNone(awaitable.cr_frame)

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

    def test_runner_uses_context_aware_fallback_for_read_only_policy_loop(self):
        runner = _BackgroundEventLoopRunner()
        original_policy = asyncio.get_event_loop_policy()

        class ReadOnlyLoop(asyncio.SelectorEventLoop):
            def __setattr__(self, name, value):
                if name == "run_in_executor":
                    raise AttributeError("run_in_executor is read-only")
                super().__setattr__(name, value)

        class Policy(asyncio.DefaultEventLoopPolicy):
            loop = None

            def new_event_loop(self):
                self.loop = ReadOnlyLoop()
                return self.loop

        policy = Policy()

        async def running_loop():
            return asyncio.get_running_loop()

        try:
            asyncio.set_event_loop_policy(policy)
            running = runner.run(running_loop())
            self.assertIsNot(running, policy.loop)
            self.assertTrue(policy.loop.is_closed())
        finally:
            runner.close()
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
