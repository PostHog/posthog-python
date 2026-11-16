from __future__ import annotations

import asyncio
import contextvars
import json
from unittest import mock

import httpx
import pytest
from freezegun import freeze_time

from posthog._async_consumer import _AsyncConsumer, _QueuedEvent, _STOP
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


@pytest.mark.asyncio
async def test_run_drops_invalid_events_and_preserves_queued_context(monkeypatch):
    marker = contextvars.ContextVar("queued_test_marker", default="outside")
    consumer = make_consumer(retries=0)
    monkeypatch.setattr("posthog._async_consumer.MAX_MSG_SIZE", 256)

    async def process(event):
        if event["event"] == "broken":
            raise ValueError("processing failed")
        if event["event"] == "filtered":
            return None
        return {**event, "context": marker.get()}

    consumer.process_event = process
    token = marker.set("queued")
    try:
        for event in [
            {"event": "broken"},
            {"event": "filtered"},
            {"event": "invalid-json", "properties": {("tuple",): True}},
            {"event": "too-large", "properties": {"large": "x" * 256}},
            {"event": "accepted"},
        ]:
            consumer.queue.put_nowait(_QueuedEvent(event, contextvars.copy_context()))
    finally:
        marker.reset(token)
    consumer.queue.put_nowait(_STOP)

    with mock.patch(
        "posthog._async_consumer.async_batch_post", new=mock.AsyncMock()
    ) as batch_post:
        await consumer.run()
        await asyncio.wait_for(consumer.queue.join(), 1)

    assert batch_post.await_count == 1
    assert batch_post.await_args.kwargs["batch"] == [
        {"event": "accepted", "context": "queued"}
    ]
    assert marker.get() == "outside"


@pytest.mark.asyncio
async def test_next_carries_oversized_batch_tail_without_reprocessing(monkeypatch):
    consumer = make_consumer(retries=0)
    events = [{"event": "first"}, {"event": "second"}]
    monkeypatch.setattr(
        "posthog._async_consumer.BATCH_SIZE_LIMIT",
        sum(len(json.dumps(event).encode()) for event in events) - 1,
    )
    for event in events:
        consumer.queue.put_nowait(_QueuedEvent(event, contextvars.copy_context()))
    consumer.queue.put_nowait(_STOP)

    first, stopped_first = await consumer.next()
    second, stopped_second = await consumer.next()

    assert first == [events[0]]
    assert second == [events[1]]
    assert (stopped_first, stopped_second) == (False, True)
    assert consumer.process_event.await_count == 2
    with mock.patch.object(consumer, "request", new=mock.AsyncMock()):
        await consumer.upload(first)
        await consumer.upload(second)
    await asyncio.wait_for(consumer.queue.join(), 1)


@pytest.mark.asyncio
async def test_upload_acknowledges_failed_batch_even_when_on_error_raises():
    consumer = make_consumer(retries=0)
    consumer.on_error = mock.AsyncMock(side_effect=RuntimeError("callback failed"))
    event = {"event": "failed"}
    consumer.queue.put_nowait(event)
    consumer.queue.get_nowait()
    error = APIError(400, "terminal")

    with mock.patch(
        "posthog._async_consumer.async_batch_post",
        new=mock.AsyncMock(side_effect=error),
    ):
        await consumer.upload([event])
    await asyncio.wait_for(consumer.queue.join(), 1)

    consumer.on_error.assert_awaited_once_with(error, [event])


@pytest.mark.asyncio
async def test_next_propagates_cancellation_during_event_processing():
    consumer = make_consumer(retries=0)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def process(event):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    consumer.process_event = process
    consumer.queue.put_nowait(
        _QueuedEvent({"event": "blocked"}, contextvars.copy_context())
    )
    task = asyncio.create_task(consumer.next())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
