from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from posthog._async_consumer import _AsyncConsumer
from posthog.capture_compression import CaptureCompression


def make_consumer(*, retries: int) -> _AsyncConsumer:
    return _AsyncConsumer(
        asyncio.Queue(),
        "test-key",
        host="https://example.com",
        on_error=None,
        process_event=mock.AsyncMock(side_effect=lambda event, defaults: event),
        flush_at=100,
        flush_interval=1,
        retries=retries,
        timeout=3,
        historical_migration=False,
        capture_compression=CaptureCompression.NONE,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("run_worker", [False, True], ids=["wait", "worker"])
async def test_get_or_flush_cancels_waiters_on_cancellation(run_worker):
    consumer = make_consumer(retries=0)
    consumer.flush_interval = 60
    wait_started = asyncio.Event()
    waiters = []
    real_wait = asyncio.wait

    async def observe_wait(tasks, **kwargs):
        waiters.extend(tasks)
        wait_started.set()
        return await real_wait(tasks, **kwargs)

    with mock.patch("posthog._async_consumer.asyncio.wait", side_effect=observe_wait):
        task = asyncio.create_task(
            consumer.run() if run_worker else consumer._get_or_flush(60)
        )
        try:
            await wait_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            assert len(waiters) == 2
            assert all(waiter.cancelled() for waiter in waiters)
        finally:
            task.cancel()
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(task, *waiters, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("queued", "flush"), [(True, False), (False, True), (False, False), (True, True)]
)
async def test_get_or_flush_preserves_results_and_cleans_up_waiters(queued, flush):
    consumer = make_consumer(retries=0)
    event = {"event": "test"}
    if queued:
        consumer.queue.put_nowait(event)
    if flush:
        consumer.request_flush()
    tasks_before = asyncio.all_tasks()

    result = await consumer._get_or_flush(60 if queued or flush else 0)

    assert result == (event if queued else None, flush and not queued)
    assert consumer._flush_event.is_set() == (queued and flush)
    assert not (asyncio.all_tasks() - tasks_before)
    if queued:
        consumer.queue.task_done()
