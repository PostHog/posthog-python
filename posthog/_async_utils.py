import asyncio
import threading
from collections.abc import Awaitable
from typing import Any


class _BackgroundEventLoopRunner:
    """Run awaitables to completion on a reusable background event loop."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._lock = threading.Lock()

    def run(self, awaitable: Awaitable[Any]) -> Any:
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(
            self._await_result(awaitable), loop
        )
        return future.result()

    def close(self) -> None:
        with self._lock:
            loop = self._loop
            thread = self._thread
            self._loop = None
            self._thread = None

        if loop is None or thread is None or loop.is_closed():
            return

        if thread is threading.current_thread():
            loop.call_soon(loop.stop)
            return

        loop.call_soon_threadsafe(loop.stop)
        thread.join()

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

            self._started.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="PostHogBackgroundEventLoopRunner",
                daemon=True,
            )
            self._thread.start()

        self._started.wait()
        with self._lock:
            assert self._loop is not None
            return self._loop

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._lock:
            self._loop = loop
            self._started.set()

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
