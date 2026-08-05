import asyncio
import sys
import threading
from collections.abc import Awaitable
from contextvars import Context, copy_context
from typing import Any


class _PlainExecutorCall:
    def __init__(self, func, args, kwargs) -> None:
        self._func = func
        self._args = args
        self._kwargs = kwargs

    def __call__(self):
        return self._func(*self._args, **self._kwargs)


class _ContextExecutorCall:
    """Carry context in-process while remaining safe for serializing executors."""

    def __init__(self, context: Context, func, args, kwargs=None) -> None:
        self._context = context
        self._func = func
        self._args = args
        self._kwargs = kwargs or {}

    def __call__(self):
        return self._context.run(self._func, *self._args, **self._kwargs)

    def __reduce__(self):
        # Context objects are not picklable and are process-local. Executors
        # that serialize work reconstruct a plain call instead.
        return (_PlainExecutorCall, (self._func, self._args, self._kwargs))


if sys.platform == "win32":
    from asyncio.windows_events import ProactorEventLoop as _PlatformEventLoop
else:
    _PlatformEventLoop = asyncio.SelectorEventLoop


class _ContextEventLoop(_PlatformEventLoop):
    def run_in_executor(self, executor, func, *args):  # type: ignore[override]
        call = _ContextExecutorCall(copy_context(), func, args)
        return super().run_in_executor(executor, call)


class _BackgroundEventLoopRunner:
    """Run awaitables to completion on a reusable background event loop."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._closing_threads: set[threading.Thread] = set()
        self._started = threading.Event()
        self._startup_error: BaseException | None = None
        self._close_requested = False
        self._lock = threading.Lock()

    def run(self, awaitable: Awaitable[Any]) -> Any:
        if threading.current_thread() is self._thread:
            raise RuntimeError("cannot synchronously run from the runner thread")

        while True:
            loop = self._ensure_loop()
            with self._lock:
                if loop is self._loop and not self._close_requested:
                    future = asyncio.run_coroutine_threadsafe(
                        self._await_result(awaitable), loop
                    )
                    break
        return future.result()

    def close(self) -> None:
        current = threading.current_thread()
        with self._lock:
            loop = self._loop
            thread = self._thread
            if thread is None:
                return
            self._close_requested = True
            if loop is None:
                self._startup_error = RuntimeError("runner closed during startup")
            else:
                self._loop = None
                self._thread = None
                self._closing_threads.add(thread)

        if loop is None:
            if thread is not current:
                thread.join()
            return

        if loop.is_closed():
            with self._lock:
                self._closing_threads.discard(thread)
            return

        if thread is current:
            loop.call_soon(loop.stop)
            return

        loop.call_soon_threadsafe(loop.stop)
        thread.join()

    def owns_thread(self, thread: threading.Thread) -> bool:
        with self._lock:
            return thread is self._thread or thread in self._closing_threads

    @staticmethod
    async def _await_result(awaitable: Awaitable[Any]) -> Any:
        return await awaitable

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if (
                self._loop is not None
                and self._thread is not None
                and self._thread.is_alive()
                and not self._loop.is_closed()
            ):
                return self._loop

            if self._thread is None or not self._thread.is_alive():
                self._started.clear()
                self._startup_error = None
                self._close_requested = False
                self._thread = threading.Thread(
                    target=self._run_loop,
                    name="PostHogBackgroundEventLoopRunner",
                    daemon=True,
                )
                self._thread.start()

        self._started.wait()
        with self._lock:
            if self._startup_error is not None:
                raise self._startup_error
            assert self._loop is not None
            return self._loop

    def _run_loop(self) -> None:
        loop = None
        try:
            loop = asyncio.new_event_loop()
            original_run_in_executor = loop.run_in_executor

            def run_in_executor(executor, func, *args):
                call = _ContextExecutorCall(copy_context(), func, args)
                return original_run_in_executor(executor, call)

            try:
                setattr(loop, "run_in_executor", run_in_executor)
            except (AttributeError, TypeError):
                # Preserve custom policy loops when they are extensible. For a
                # read-only implementation, use the platform-equivalent loop
                # so explicit executors retain callback context safely.
                loop.close()
                loop = _ContextEventLoop()
            asyncio.set_event_loop(loop)
        except BaseException as error:
            if loop is not None and not loop.is_closed():
                loop.close()
            with self._lock:
                self._startup_error = error
                self._thread = None
                self._started.set()
            return

        with self._lock:
            self._loop = loop
            close_requested = self._close_requested
            if close_requested and self._startup_error is None:
                self._startup_error = RuntimeError("runner closed during startup")
            self._started.set()

        if close_requested:
            loop.call_soon(loop.stop)

        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
            asyncio.set_event_loop(None)
            loop.close()
            current = threading.current_thread()
            with self._lock:
                if self._thread is current:
                    self._thread = None
                if self._loop is loop:
                    self._loop = None
                self._closing_threads.discard(current)
