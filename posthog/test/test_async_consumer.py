from __future__ import annotations

import asyncio
import json
from unittest import mock

import httpx
import pytest
from freezegun import freeze_time

from posthog._async_consumer import _AsyncConsumer
from posthog.capture_compression import CaptureCompression
from posthog.capture_mode import CaptureMode
from posthog.request import APIError


def make_consumer(*, retries: int) -> _AsyncConsumer:
    return _AsyncConsumer(
        asyncio.Queue(),
        "test-key",
        host="https://example.com",
        on_error=None,
        process_event=mock.AsyncMock(side_effect=lambda event: event),
        flush_at=100,
        flush_interval=1,
        gzip=False,
        retries=retries,
        timeout=3,
        historical_migration=False,
        capture_mode=CaptureMode.V0,
        capture_compression=CaptureCompression.NONE,
        http_client=mock.Mock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failures", "retry_after", "expected_delays"),
    [
        (1, None, [1]),
        (2, None, [1, 2]),
        (2, 5, [5, 5]),
    ],
)
async def test_request_retries_transient_failures_until_success(
    failures, retry_after, expected_delays
):
    error = APIError(503, "temporary", retry_after=retry_after)
    consumer = make_consumer(retries=failures)

    with (
        mock.patch(
            "posthog._async_consumer.async_batch_post",
            new=mock.AsyncMock(side_effect=[error] * failures + [None]),
        ) as batch_post,
        mock.patch(
            "posthog._async_consumer.asyncio.sleep", new=mock.AsyncMock()
        ) as sleep,
    ):
        await consumer.request([{"event": "test"}])

    assert batch_post.await_count == failures + 1
    assert [call.args[0] for call in sleep.await_args_list] == expected_delays


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retry_after", "expected_delay"),
    [
        ("Tue, 08 Sep 2026 00:00:10 GMT", 10),
        ("Tue, 08 Sep 2026 00:01:00 GMT", 30),
        ("Mon, 07 Sep 2026 23:59:59 GMT", 1),
        ("5", 5),
        ("0", 1),
        ("-1", 1),
        ("invalid", 1),
        (None, 1),
    ],
)
async def test_request_honors_retry_after_from_http_response(
    retry_after, expected_delay
):
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    responses = [
        httpx.Response(503, headers=headers, json={"detail": "temporary"}),
        httpx.Response(200, json={"ok": True}),
    ]
    requests = []

    def handle_request(request):
        requests.append(request)
        return responses.pop(0)

    consumer = make_consumer(retries=1)
    batch = [{"event": "test", "distinct_id": "test-user"}]
    async with httpx.AsyncClient(
        base_url="https://example.com", transport=httpx.MockTransport(handle_request)
    ) as client:
        consumer.http_client = client
        with (
            freeze_time("2026-09-08 00:00:00", real_asyncio=True),
            mock.patch(
                "posthog._async_consumer.asyncio.sleep", new=mock.AsyncMock()
            ) as sleep,
        ):
            await consumer.request(batch)

    sleep.assert_awaited_once_with(expected_delay)
    assert len(requests) == 2
    assert [json.loads(request.content)["batch"] for request in requests] == [
        batch,
        batch,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 413])
async def test_request_does_not_retry_terminal_client_errors(status):
    consumer = make_consumer(retries=3)

    with (
        mock.patch(
            "posthog._async_consumer.async_batch_post",
            new=mock.AsyncMock(side_effect=APIError(status, "terminal")),
        ) as batch_post,
        mock.patch(
            "posthog._async_consumer.asyncio.sleep", new=mock.AsyncMock()
        ) as sleep,
        pytest.raises(APIError),
    ):
        await consumer.request([{"event": "test"}])

    batch_post.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_stops_after_configured_retry_limit():
    consumer = make_consumer(retries=2)

    with (
        mock.patch(
            "posthog._async_consumer.async_batch_post",
            new=mock.AsyncMock(side_effect=APIError(503, "temporary")),
        ) as batch_post,
        mock.patch(
            "posthog._async_consumer.asyncio.sleep", new=mock.AsyncMock()
        ) as sleep,
        pytest.raises(APIError),
    ):
        await consumer.request([{"event": "test"}])

    assert batch_post.await_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [1, 2]


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
