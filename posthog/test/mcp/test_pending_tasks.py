import asyncio
import threading

from posthog.mcp import PostHogMCP
from posthog.mcp import _instrumentation as instrumentation


async def test_async_drain_is_scoped_to_owner():
    first_owner = object()
    second_owner = object()
    first_done = []
    second_started = asyncio.Event()
    release_second = asyncio.Event()

    async def first_capture():
        await asyncio.sleep(0)
        first_done.append(True)

    async def second_capture():
        second_started.set()
        await release_second.wait()

    instrumentation.fire_and_forget(first_capture(), first_owner)
    instrumentation.fire_and_forget(second_capture(), second_owner)
    await second_started.wait()

    await asyncio.wait_for(instrumentation.drain_pending(first_owner), timeout=1)

    assert first_done == [True]
    assert not release_second.is_set()

    release_second.set()
    await instrumentation.drain_pending(second_owner)


async def test_async_drain_ignores_same_owner_tasks_on_another_loop():
    owner = object()
    foreign_started = threading.Event()
    release_foreign = threading.Event()
    foreign_done = threading.Event()
    thread_errors = []

    async def foreign_capture():
        foreign_started.set()
        while not release_foreign.is_set():
            await asyncio.sleep(0.01)
        foreign_done.set()

    def run_foreign_loop():
        async def run():
            instrumentation.fire_and_forget(foreign_capture(), owner)
            while not release_foreign.is_set():
                await asyncio.sleep(0.01)
            await instrumentation.drain_pending(owner)

        try:
            asyncio.run(run())
        except BaseException as error:  # noqa: BLE001 - surfaced in the test thread
            thread_errors.append(error)

    thread = threading.Thread(target=run_foreign_loop)
    thread.start()
    assert foreign_started.wait(timeout=1)

    local_done = []

    async def local_capture():
        await asyncio.sleep(0)
        local_done.append(True)

    try:
        instrumentation.fire_and_forget(local_capture(), owner)
        await asyncio.wait_for(instrumentation.drain_pending(owner), timeout=1)

        assert local_done == [True]
        assert not foreign_done.is_set()
        assert thread_errors == []
    finally:
        release_foreign.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert foreign_done.is_set()
    assert thread_errors == []


def test_sync_drain_is_scoped_to_owner():
    first_owner = object()
    second_owner = object()
    first_done = []
    second_started = threading.Event()
    release_second = threading.Event()
    second_done = threading.Event()

    async def first_capture():
        await asyncio.sleep(0)
        first_done.append(True)

    async def second_capture():
        second_started.set()
        while not release_second.is_set():
            await asyncio.sleep(0.01)
        second_done.set()

    instrumentation.fire_and_forget(first_capture(), first_owner, background=True)
    instrumentation.fire_and_forget(second_capture(), second_owner, background=True)
    assert second_started.wait(timeout=1)

    try:
        instrumentation.drain_pending_sync(first_owner, timeout=1)

        assert first_done == [True]
        assert not second_done.is_set()
    finally:
        release_second.set()
        instrumentation.drain_pending_sync(second_owner, timeout=2)

    assert second_done.is_set()


async def test_posthog_mcp_sync_flush_drains_capture_from_async_host(monkeypatch):
    client = PostHogMCP("phc_test", disabled=True)
    captured = []
    flushed_after = []
    client.capture = lambda event, **kwargs: captured.append({"event": event, **kwargs})

    def record_flush(self, timeout_seconds=10):
        flushed_after.append(list(captured))

    monkeypatch.setattr("posthog.client.Client.flush", record_flush)

    client.capture_tool_call("search")
    client.flush(timeout_seconds=1)

    assert captured[0]["event"] == "$mcp_tool_call"
    assert flushed_after == [captured]
