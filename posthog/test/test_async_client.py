from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from posthog import AsyncClient, AsyncPostHog


@pytest.mark.asyncio
async def test_async_posthog_exports_async_client_alias():
    client = AsyncPostHog("test-key", send=False)
    assert isinstance(client, AsyncClient)
    await client.shutdown()


@pytest.mark.asyncio
async def test_capture_enqueues_and_flushes_with_async_consumer():
    batches = []

    async def mock_batch_post(*args, **kwargs):
        batches.append(kwargs["batch"])

    with mock.patch(
        "posthog._async_consumer.async_batch_post", side_effect=mock_batch_post
    ):
        async with AsyncPostHog(
            "test-key",
            host="https://example.com",
            flush_at=1,
            flush_interval=0.01,
        ) as client:
            event_uuid = await client.capture(
                "async event",
                distinct_id="user-1",
                properties={"plan": "pro"},
            )
            assert event_uuid is not None
            await client.flush(timeout_seconds=1)

    assert len(batches) == 1
    event = batches[0][0]
    assert event["event"] == "async event"
    assert event["distinct_id"] == "user-1"
    assert event["properties"]["plan"] == "pro"
    assert event["properties"]["$lib"] == "posthog-python"
    assert event["properties"]["$geoip_disable"] is True
    assert event["uuid"] == event_uuid


@pytest.mark.asyncio
async def test_capture_supports_async_before_send():
    batches = []

    async def before_send(event):
        event["properties"]["from_before_send"] = True
        return event

    async def mock_batch_post(*args, **kwargs):
        batches.append(kwargs["batch"])

    with mock.patch(
        "posthog._async_consumer.async_batch_post", side_effect=mock_batch_post
    ):
        async with AsyncPostHog(
            "test-key",
            before_send=before_send,
            flush_at=1,
            flush_interval=0.01,
        ) as client:
            await client.capture("async event", distinct_id="user-1")
            await client.flush(timeout_seconds=1)

    assert batches[0][0]["properties"]["from_before_send"] is True


@pytest.mark.asyncio
async def test_capture_drops_when_async_before_send_returns_none():
    async def before_send(event):
        return None

    with mock.patch("posthog._async_consumer.async_batch_post") as mock_batch_post:
        async with AsyncPostHog(
            "test-key",
            before_send=before_send,
            flush_at=1,
            flush_interval=0.01,
        ) as client:
            result = await client.capture("drop me", distinct_id="user-1")
            await client.flush(timeout_seconds=1)

    assert result is None
    mock_batch_post.assert_not_called()


@pytest.mark.asyncio
async def test_capture_send_false_returns_uuid_without_starting_workers():
    client = AsyncPostHog("test-key", send=False)

    event_uuid = await client.capture("async event", distinct_id="user-1")

    assert event_uuid is not None
    assert client._worker_tasks == []
    await client.shutdown()


@pytest.mark.asyncio
async def test_sync_mode_awaits_direct_batch_post():
    batches = []

    async def mock_batch_post(*args, **kwargs):
        batches.append(kwargs["batch"])

    with mock.patch(
        "posthog.async_client._async_batch_post", side_effect=mock_batch_post
    ):
        client = AsyncPostHog("test-key", sync_mode=True)
        event_uuid = await client.capture("sync mode event", distinct_id="user-1")
        await client.shutdown()

    assert event_uuid is not None
    assert batches[0][0]["event"] == "sync mode event"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "method_kwargs", "expected_event"),
    [
        (
            "set",
            {"distinct_id": "user-1", "properties": {"email": "a@example.com"}},
            "$set",
        ),
        (
            "set_once",
            {"distinct_id": "user-1", "properties": {"first_seen": True}},
            "$set_once",
        ),
        (
            "alias",
            {"previous_id": "anon-1", "distinct_id": "user-1"},
            "$create_alias",
        ),
    ],
)
async def test_async_identify_methods_enqueue_events(
    method_name, method_kwargs, expected_event
):
    batches = []

    async def mock_batch_post(*args, **kwargs):
        batches.append(kwargs["batch"])

    with mock.patch(
        "posthog._async_consumer.async_batch_post", side_effect=mock_batch_post
    ):
        async with AsyncPostHog("test-key", flush_at=1, flush_interval=0.01) as client:
            method = getattr(client, method_name)
            await method(**method_kwargs)
            await client.flush(timeout_seconds=1)

    assert batches[0][0]["event"] == expected_event


@pytest.mark.asyncio
async def test_capture_send_feature_flags_uses_async_flag_evaluation():
    client = AsyncPostHog("test-key", send=False)

    with mock.patch.object(
        AsyncPostHog, "get_all_flags", mock.AsyncMock(return_value={"beta": True})
    ) as get_all_flags:
        event_uuid = await client.capture(
            "async event", distinct_id="user-1", send_feature_flags=True
        )

    assert event_uuid is not None
    get_all_flags.assert_awaited_once()
    await client.shutdown()


@pytest.mark.asyncio
async def test_reuses_owned_http_client_and_closes_it_on_shutdown():
    http_client = mock.Mock()
    http_client.aclose = mock.AsyncMock()

    with (
        mock.patch(
            "posthog.async_client._build_client", return_value=http_client
        ) as build,
        mock.patch(
            "posthog.async_client._async_batch_post", new=mock.AsyncMock()
        ) as batch_post,
    ):
        client = AsyncPostHog("test-key", sync_mode=True)
        await client.capture("first", distinct_id="user-1")
        await client.capture("second", distinct_id="user-1")
        await client.shutdown()

    build.assert_called_once_with()
    assert [call.kwargs["client"] for call in batch_post.await_args_list] == [
        http_client,
        http_client,
    ]
    http_client.aclose.assert_awaited_once_with()


def test_separate_event_loops_use_separate_http_clients():
    http_clients = []

    def build_http_client():
        http_client = mock.Mock()
        http_client.aclose = mock.AsyncMock()
        http_clients.append(http_client)
        return http_client

    async def capture_and_shutdown():
        client = AsyncPostHog("test-key", sync_mode=True)
        await client.capture("event", distinct_id="user-1")
        await client.shutdown()

    with (
        mock.patch("posthog.async_client._build_client", side_effect=build_http_client),
        mock.patch("posthog.async_client._async_batch_post", new=mock.AsyncMock()),
    ):
        asyncio.run(capture_and_shutdown())
        asyncio.run(capture_and_shutdown())

    assert len(http_clients) == 2
    assert http_clients[0] is not http_clients[1]
    for http_client in http_clients:
        http_client.aclose.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_mode", [False, True])
async def test_capture_after_shutdown_does_not_restart_transport(sync_mode):
    with (
        mock.patch("posthog.async_client._build_client") as build,
        mock.patch("posthog.async_client._async_batch_post", new=mock.AsyncMock()),
    ):
        client = AsyncPostHog("test-key", sync_mode=sync_mode)
        await client.shutdown()
        result = await client.capture("event", distinct_id="user-1")

    assert result is None
    build.assert_not_called()
    assert client._worker_tasks == []


@pytest.mark.asyncio
async def test_shutdown_offloads_blocking_client_cleanup():
    client = AsyncPostHog("test-key", send=False)
    client.poller = mock.Mock()
    offloaded_functions = []

    async def fake_to_thread(fn, *args, **kwargs):
        offloaded_functions.append(fn.__name__)
        return fn(*args, **kwargs)

    with mock.patch(
        "posthog.async_client.asyncio.to_thread", side_effect=fake_to_thread
    ):
        await client.shutdown()

    assert "_stop_blocking_polling_resources" in offloaded_functions
    assert "_unregister_duplicate_client" in offloaded_functions
    client.poller.stop.assert_called_once_with()
