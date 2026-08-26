from __future__ import annotations

import asyncio
import logging
from unittest import mock

import pytest

from posthog import AsyncClient, AsyncPosthog, CaptureCompression, CaptureMode
from posthog.request import APIError


@pytest.mark.asyncio
async def test_debug_logging_does_not_leak_to_later_clients():
    debug_client = AsyncPosthog("test-key", send=False, debug=True)
    assert debug_client.log.level == logging.DEBUG
    await debug_client.shutdown()

    normal_client = AsyncPosthog("test-key", send=False)
    assert normal_client.log.level == logging.WARNING
    await normal_client.shutdown()


@pytest.mark.asyncio
async def test_async_posthog_is_the_customer_facing_async_client():
    client = AsyncPosthog("test-key", send=False)
    assert isinstance(client, AsyncClient)
    await client.shutdown()


@pytest.mark.asyncio
async def test_capture_is_a_synchronous_queue_write_and_flushes():
    batches = []

    async def batch_post(*args, **kwargs):
        batches.append(kwargs["batch"])

    with mock.patch("posthog._async_consumer.async_batch_post", side_effect=batch_post):
        async with AsyncPosthog("test-key", flush_at=100, flush_interval=30) as client:
            event_uuid = client.capture(
                "async event",
                distinct_id="user-1",
                properties={"plan": "pro"},
            )
            assert isinstance(event_uuid, str)
            await client.flush(timeout_seconds=1)

    assert len(batches) == 1
    event = batches[0][0]
    assert event["event"] == "async event"
    assert event["distinct_id"] == "user-1"
    assert event["properties"]["plan"] == "pro"
    assert event["properties"]["$lib"] == "posthog-python"
    assert event["properties"]["$geoip_disable"] is True
    assert event["properties"]["$is_server"] is True
    assert event["uuid"] == event_uuid


@pytest.mark.asyncio
async def test_capture_runs_async_before_send_in_consumer():
    batches = []

    async def before_send(event):
        await asyncio.sleep(0)
        event["properties"]["from_before_send"] = True
        return event

    async def batch_post(*args, **kwargs):
        batches.append(kwargs["batch"])

    with mock.patch("posthog._async_consumer.async_batch_post", side_effect=batch_post):
        async with AsyncPosthog(
            "test-key", before_send=before_send, flush_interval=30
        ) as client:
            event_uuid = client.capture("event", distinct_id="user-1")
            await client.flush(timeout_seconds=1)

    assert batches[0][0]["properties"]["from_before_send"] is True
    assert batches[0][0]["uuid"] == event_uuid


@pytest.mark.asyncio
async def test_capture_drops_event_when_before_send_raises():
    async def before_send(_event):
        raise RuntimeError("callback failed")

    with mock.patch(
        "posthog._async_consumer.async_batch_post", new=mock.AsyncMock()
    ) as batch_post:
        async with AsyncPosthog(
            "test-key", before_send=before_send, flush_interval=0.01
        ) as client:
            accepted_uuid = client.capture("event", distinct_id="user-1")
            await client.flush(timeout_seconds=1)

    assert accepted_uuid is not None
    batch_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_immediate_waits_for_delivery():
    delivered = asyncio.Event()

    async def batch_post(*args, **kwargs):
        assert kwargs["batch"][0]["event"] == "immediate event"
        delivered.set()

    with mock.patch("posthog._async_consumer.async_batch_post", side_effect=batch_post):
        client = AsyncPosthog("test-key")
        event_uuid = await client.capture_immediate(
            "immediate event", distinct_id="user-1"
        )
        assert delivered.is_set()
        assert event_uuid is not None
        await client.shutdown()


@pytest.mark.asyncio
async def test_capture_immediate_supports_async_before_send():
    batches = []

    async def before_send(event):
        await asyncio.sleep(0)
        event["properties"]["processed"] = True
        return event

    async def batch_post(*args, **kwargs):
        batches.append(kwargs["batch"])

    with mock.patch("posthog._async_consumer.async_batch_post", side_effect=batch_post):
        client = AsyncPosthog("test-key", before_send=before_send)
        result = await client.capture_immediate("event", distinct_id="user-1")
        await client.shutdown()

    assert result is not None
    assert batches[0][0]["properties"]["processed"] is True


@pytest.mark.asyncio
async def test_capture_immediate_uses_capture_v1_without_building_httpx_client():
    with (
        mock.patch(
            "posthog._async_consumer.async_send_v1_batch", new=mock.AsyncMock()
        ) as send_v1,
        mock.patch("posthog.async_client._build_client") as build_client,
    ):
        client = AsyncPosthog(
            "test-key",
            capture_mode=CaptureMode.V1,
            capture_compression=CaptureCompression.GZIP,
        )
        event_uuid = await client.capture_immediate("event", distinct_id="user-1")
        await client.shutdown()

    assert event_uuid is not None
    build_client.assert_not_called()
    send_v1.assert_awaited_once()
    assert send_v1.await_args.kwargs["compression"] == CaptureCompression.GZIP
    assert send_v1.await_args.args[2][0]["uuid"] == event_uuid


@pytest.mark.asyncio
async def test_send_false_accepts_without_starting_workers_or_transport():
    with mock.patch("posthog.async_client._build_client") as build_client:
        client = AsyncPosthog("test-key", send=False)
        assert client.capture("event", distinct_id="user-1") is not None
        assert await client.capture_immediate("event", distinct_id="user-1") is not None
        assert client._worker_tasks == []
        await client.shutdown()
    build_client.assert_not_called()


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
        (
            "group_identify",
            {"group_type": "company", "group_key": "company-1"},
            "$groupidentify",
        ),
    ],
)
async def test_identify_methods_enqueue_events(
    method_name, method_kwargs, expected_event
):
    batches = []

    async def batch_post(*args, **kwargs):
        batches.append(kwargs["batch"])

    with mock.patch("posthog._async_consumer.async_batch_post", side_effect=batch_post):
        async with AsyncPosthog("test-key", flush_interval=30) as client:
            method = getattr(client, method_name)
            assert method(**method_kwargs) is not None
            await client.flush(timeout_seconds=1)

    assert batches[0][0]["event"] == expected_event


@pytest.mark.asyncio
async def test_capture_after_shutdown_is_dropped_without_restarting_workers():
    with mock.patch("posthog.async_client._build_client") as build_client:
        client = AsyncPosthog("test-key")
        await client.shutdown()
        assert client.capture("event", distinct_id="user-1") is None
        assert await client.capture_immediate("event", distinct_id="user-1") is None
        assert client._worker_tasks == []
    build_client.assert_not_called()


@pytest.mark.asyncio
async def test_batch_size_overflow_event_is_sent_in_the_next_batch():
    batches = []

    async def batch_post(*args, **kwargs):
        batches.append(kwargs["batch"])

    with (
        mock.patch("posthog._async_consumer.BATCH_SIZE_LIMIT", 800),
        mock.patch("posthog._async_consumer.async_batch_post", side_effect=batch_post),
    ):
        client = AsyncPosthog("test-key", flush_at=10, flush_interval=30)
        client.capture("first", distinct_id="user-1", properties={"value": "a" * 400})
        client.capture("second", distinct_id="user-1", properties={"value": "b" * 400})
        await client.flush(timeout_seconds=1)
        await client.shutdown()

    assert [[event["event"] for event in batch] for batch in batches] == [
        ["first"],
        ["second"],
    ]


@pytest.mark.asyncio
async def test_shutdown_waits_for_an_in_flight_batch_instead_of_cancelling_it():
    upload_started = asyncio.Event()
    allow_upload = asyncio.Event()
    delivered = []

    async def batch_post(*args, **kwargs):
        upload_started.set()
        await allow_upload.wait()
        delivered.extend(kwargs["batch"])

    with mock.patch("posthog._async_consumer.async_batch_post", side_effect=batch_post):
        client = AsyncPosthog("test-key", flush_at=1)
        client.capture("event", distinct_id="user-1")
        await upload_started.wait()
        shutdown = asyncio.create_task(client.shutdown())
        await asyncio.sleep(0)
        assert not shutdown.done()
        allow_upload.set()
        await shutdown

    assert [event["event"] for event in delivered] == ["event"]


@pytest.mark.asyncio
async def test_shutdown_called_from_before_send_is_deferred_without_deadlock():
    callback_finished = asyncio.Event()
    client: AsyncPosthog | None = None

    async def before_send(event):
        assert client is not None
        await client.shutdown()
        callback_finished.set()
        return event

    with mock.patch(
        "posthog._async_consumer.async_batch_post", new=mock.AsyncMock()
    ) as batch_post:
        client = AsyncPosthog("test-key", before_send=before_send, flush_at=1)
        client.capture("event", distinct_id="user-1")
        await asyncio.wait_for(callback_finished.wait(), timeout=1)
        await asyncio.wait_for(client.shutdown(), timeout=1)

    batch_post.assert_awaited_once()
    assert client.capture("after shutdown", distinct_id="user-1") is None


@pytest.mark.asyncio
async def test_external_shutdown_delivers_event_already_in_before_send():
    callback_started = asyncio.Event()
    allow_callback = asyncio.Event()
    delivered = []

    async def before_send(event):
        callback_started.set()
        await allow_callback.wait()
        return event

    async def batch_post(*args, **kwargs):
        delivered.extend(kwargs["batch"])

    with mock.patch("posthog._async_consumer.async_batch_post", side_effect=batch_post):
        client = AsyncPosthog("test-key", before_send=before_send, flush_at=1)
        client.capture("event", distinct_id="user-1")
        await callback_started.wait()
        shutdown = asyncio.create_task(client.shutdown())
        await asyncio.sleep(0)
        allow_callback.set()
        await shutdown

    assert [event["event"] for event in delivered] == ["event"]


@pytest.mark.asyncio
async def test_shutdown_waits_for_immediate_operation_not_its_long_lived_caller():
    upload_started = asyncio.Event()
    allow_upload = asyncio.Event()
    shutdown_task = None

    async def batch_post(*args, **kwargs):
        upload_started.set()
        await allow_upload.wait()

    async def capture_then_await_shutdown(client):
        result = await client.capture_immediate("event", distinct_id="user-1")
        assert result is not None
        assert shutdown_task is not None
        await shutdown_task

    with mock.patch("posthog._async_consumer.async_batch_post", side_effect=batch_post):
        client = AsyncPosthog("test-key")
        caller = asyncio.create_task(capture_then_await_shutdown(client))
        await upload_started.wait()
        shutdown_task = asyncio.create_task(client.shutdown())
        allow_upload.set()
        await asyncio.wait_for(caller, timeout=1)


@pytest.mark.asyncio
async def test_shutdown_waits_for_in_flight_immediate_capture():
    upload_started = asyncio.Event()
    allow_upload = asyncio.Event()

    async def batch_post(*args, **kwargs):
        upload_started.set()
        await allow_upload.wait()

    with mock.patch("posthog._async_consumer.async_batch_post", side_effect=batch_post):
        client = AsyncPosthog("test-key")
        capture = asyncio.create_task(
            client.capture_immediate("event", distinct_id="user-1")
        )
        await upload_started.wait()
        shutdown = asyncio.create_task(client.shutdown())
        await asyncio.sleep(0)
        assert not shutdown.done()
        allow_upload.set()
        assert await capture is not None
        await shutdown


@pytest.mark.asyncio
async def test_reuses_and_closes_instance_owned_http_client():
    http_client = mock.Mock()
    http_client.aclose = mock.AsyncMock()

    with (
        mock.patch(
            "posthog.async_client._build_client", return_value=http_client
        ) as build,
        mock.patch(
            "posthog._async_consumer.async_batch_post", new=mock.AsyncMock()
        ) as batch_post,
    ):
        client = AsyncPosthog("test-key")
        await client.capture_immediate("first", distinct_id="user-1")
        await client.capture_immediate("second", distinct_id="user-1")
        await client.shutdown()

    build.assert_called_once_with(client.host)
    assert [call.kwargs["client"] for call in batch_post.await_args_list] == [
        http_client,
        http_client,
    ]
    http_client.aclose.assert_awaited_once_with()


def test_capture_before_loop_starts_is_flushed_when_loop_runs():
    batches = []
    client = AsyncPosthog("test-key", flush_interval=30)
    assert client.capture("event", distinct_id="user-1") is not None

    async def batch_post(*args, **kwargs):
        batches.append(kwargs["batch"])

    async def flush_and_close():
        with mock.patch(
            "posthog._async_consumer.async_batch_post", side_effect=batch_post
        ):
            await client.shutdown()

    asyncio.run(flush_and_close())
    assert batches[0][0]["event"] == "event"


@pytest.mark.parametrize(("option", "value"), [("flush_at", 0), ("flush_interval", 0)])
def test_rejects_non_positive_batch_settings(option, value):
    with pytest.raises(ValueError, match=option):
        AsyncPosthog("test-key", **{option: value})


@pytest.mark.asyncio
async def test_capture_exception_never_raises_in_debug_mode():
    client = AsyncPosthog("test-key", send=False, debug=True)
    with mock.patch.object(client, "capture", side_effect=RuntimeError("broken")):
        assert client.capture_exception(ValueError("boom")) is None
    await client.shutdown()


@pytest.mark.asyncio
async def test_queued_payload_is_not_written_to_debug_logs(caplog):
    caplog.set_level(logging.DEBUG, logger="posthog")
    with mock.patch("posthog._async_consumer.async_batch_post", new=mock.AsyncMock()):
        async with AsyncPosthog("test-key", flush_interval=30) as client:
            client.capture(
                "event",
                distinct_id="user-1",
                properties={"password": "super-secret"},
            )
            await client.flush(timeout_seconds=1)

    assert "super-secret" not in caplog.text
    assert "test-key" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("immediate", [False, True])
async def test_failed_capture_does_not_log_server_response_detail(caplog, immediate):
    caplog.set_level(logging.DEBUG, logger="posthog")
    server_error = APIError(400, "password=server-secret")

    with mock.patch(
        "posthog._async_consumer.async_batch_post", side_effect=server_error
    ):
        client = AsyncPosthog("test-key", flush_at=1, max_retries=0)
        if immediate:
            await client.capture_immediate("event", distinct_id="user-1")
        else:
            client.capture("event", distinct_id="user-1")
            await client.flush(timeout_seconds=1)
        await client.shutdown()

    assert "server-secret" not in caplog.text
    assert "APIError" in caplog.text
    assert "status=400" in caplog.text


@pytest.mark.asyncio
async def test_flush_timeout_reports_unfinished_items(caplog):
    upload_started = asyncio.Event()
    allow_upload = asyncio.Event()

    async def batch_post(*args, **kwargs):
        upload_started.set()
        await allow_upload.wait()

    with mock.patch("posthog._async_consumer.async_batch_post", side_effect=batch_post):
        client = AsyncPosthog("test-key", flush_at=1)
        client.capture("event", distinct_id="user-1")
        await upload_started.wait()
        await client.flush(timeout_seconds=0.01)
        assert "items pending" in caplog.text
        allow_upload.set()
        await client.shutdown()
