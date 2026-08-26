from __future__ import annotations

import asyncio
from unittest import mock

import pytest

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
