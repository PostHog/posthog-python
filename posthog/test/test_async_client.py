from __future__ import annotations

import asyncio
import contextvars
import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID
import subprocess
import sys
import threading
from unittest import mock

import pytest

from posthog import AsyncClient, AsyncPosthog, CaptureCompression, CaptureMode
from posthog.consumer import MAX_MSG_SIZE
from posthog.contexts import (
    identify_context,
    new_context,
    set_context_session,
    set_capture_exception_code_variables_context,
    set_code_variables_detect_secrets_context,
    set_code_variables_ignore_patterns_context,
    set_code_variables_mask_patterns_context,
    set_code_variables_mask_url_credentials_context,
)
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


def test_capture_from_worker_thread_wakes_loop_bound_queue():
    script = r"""
import asyncio
import os
import threading
from unittest import mock
from posthog import AsyncPosthog

async def main():
    delivered = []

    async def batch_post(*args, **kwargs):
        delivered.extend(kwargs["batch"])

    with mock.patch(
        "posthog._async_consumer.async_batch_post", side_effect=batch_post
    ):
        client = AsyncPosthog("test-key", flush_interval=30)
        client._ensure_workers_started()
        while not client._queue._getters:
            await asyncio.sleep(0)

        asyncio.get_running_loop().set_debug(True)
        capture_result = []
        capture_thread = threading.Thread(
            target=lambda: capture_result.append(
                client.capture("threaded event", distinct_id="user-1")
            ),
            daemon=True,
        )
        capture_thread.start()
        for _ in range(100):
            if not capture_thread.is_alive():
                break
            await asyncio.sleep(0.01)

        if capture_thread.is_alive():
            os._exit(2)
        if len(capture_result) != 1 or capture_result[0] is None:
            os._exit(3)

        await asyncio.wait_for(client.flush(), timeout=1)
        await client.shutdown()
        if [event["event"] for event in delivered] != ["threaded event"]:
            os._exit(4)

asyncio.run(main())
"""
    subprocess.run([sys.executable, "-c", script], check=True, timeout=5)


@pytest.mark.asyncio
async def test_cross_thread_capture_rechecks_shutdown_before_queue_admission():
    client = AsyncPosthog("test-key", flush_interval=30)
    client._ensure_workers_started()
    scheduled_callbacks = []
    capture_result = []

    with mock.patch.object(
        client._loop,
        "call_soon_threadsafe",
        side_effect=lambda callback: scheduled_callbacks.append(callback),
    ):
        capture_thread = threading.Thread(
            target=lambda: capture_result.append(
                client.capture("threaded event", distinct_id="user-1")
            ),
            daemon=True,
        )
        capture_thread.start()
        while not scheduled_callbacks:
            await asyncio.sleep(0)

        client._accepting = False
        scheduled_callbacks.pop()()
        capture_thread.join(timeout=1)

    assert capture_result == [None]
    for task in client._worker_tasks:
        task.cancel()
    await asyncio.gather(*client._worker_tasks, return_exceptions=True)
    await client._close_transport()


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
async def test_capture_preserves_context_for_each_buffered_event():
    request_id = contextvars.ContextVar("request_id")
    batches = []

    def before_send(event):
        event["properties"]["request_id"] = request_id.get()
        return event

    async def batch_post(*args, **kwargs):
        batches.append(kwargs["batch"])

    with mock.patch("posthog._async_consumer.async_batch_post", side_effect=batch_post):
        client = AsyncPosthog("test-key", before_send=before_send, flush_interval=30)
        token = request_id.set("request-A")
        client.capture("event-A", distinct_id="user-1")
        request_id.reset(token)

        token = request_id.set("request-B")
        client.capture("event-B", distinct_id="user-1")
        request_id.reset(token)

        await client.flush(timeout_seconds=1)
        await client.shutdown()

    events = [event for batch in batches for event in batch]
    assert [event["properties"]["request_id"] for event in events] == [
        "request-A",
        "request-B",
    ]


@pytest.mark.asyncio
async def test_capture_offloads_synchronous_before_send():
    callback_thread = None

    def before_send(event):
        nonlocal callback_thread
        callback_thread = threading.get_ident()
        return event

    with mock.patch("posthog._async_consumer.async_batch_post", new=mock.AsyncMock()):
        client = AsyncPosthog("test-key", before_send=before_send)
        result = await client.capture_immediate("event", distinct_id="user-1")
        await client.shutdown()

    assert result is not None
    assert callback_thread != threading.get_ident()


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
async def test_capture_immediate_drops_oversized_event_after_before_send():
    def before_send(event):
        event["properties"]["user_input"] = "x" * MAX_MSG_SIZE
        return event

    with mock.patch(
        "posthog._async_consumer.async_batch_post", new=mock.AsyncMock()
    ) as batch_post:
        client = AsyncPosthog("test-key", before_send=before_send)
        result = await client.capture_immediate("event", distinct_id="user-1")
        await client.shutdown()

    assert result is None
    batch_post.assert_not_awaited()


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
async def test_missing_async_extra_does_not_accept_undeliverable_events():
    with mock.patch(
        "posthog.async_client._require_httpx",
        side_effect=RuntimeError("install posthog[async]"),
    ):
        client = AsyncPosthog("test-key")
        assert client.capture("event", distinct_id="user-1") is None
        assert (
            client.set(distinct_id="user-1", properties={"email": "a@example.com"})
            is None
        )
        assert client._pending_queue_items() == 0
        assert client._worker_tasks == []
        await client.shutdown()


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
async def test_flush_wakes_each_worker_with_a_partial_batch():
    slow_callback_started = asyncio.Event()
    allow_slow_callback = asyncio.Event()
    delivered = []

    async def before_send(event):
        if event["event"] == "slow":
            slow_callback_started.set()
            await allow_slow_callback.wait()
        return event

    async def batch_post(*args, **kwargs):
        delivered.extend(kwargs["batch"])

    with mock.patch("posthog._async_consumer.async_batch_post", side_effect=batch_post):
        client = AsyncPosthog(
            "test-key",
            thread=2,
            flush_at=100,
            flush_interval=30,
            before_send=before_send,
        )
        client.capture("slow", distinct_id="user-1")
        await slow_callback_started.wait()
        client.capture("fast", distinct_id="user-1")
        flush = asyncio.create_task(client.flush(timeout_seconds=1))
        await asyncio.sleep(0)
        allow_slow_callback.set()
        await flush
        await client.shutdown()

    assert sorted(event["event"] for event in delivered) == ["fast", "slow"]


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

        async def wait_until_closed():
            while not client._closed:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_until_closed(), timeout=1)

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
async def test_shutdown_returns_when_all_workers_exited_with_queued_work():
    client = AsyncPosthog("test-key", flush_interval=30)
    client._ensure_workers_started()
    for task in client._worker_tasks:
        task.cancel()
    await asyncio.gather(*client._worker_tasks, return_exceptions=True)

    assert client.capture("undrainable event", distinct_id="user-1") is not None
    try:
        await asyncio.wait_for(client.shutdown(), timeout=0.1)
    finally:
        if client._http_client is not None:
            await client._http_client.aclose()


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
async def test_capture_exception_uses_context_code_variable_settings():
    client = AsyncPosthog(
        "test-key",
        send=False,
        capture_exception_code_variables=False,
    )
    with (
        new_context(),
        mock.patch(
            "posthog.async_client.try_attach_code_variables_to_frames"
        ) as attach,
    ):
        set_capture_exception_code_variables_context(True)
        set_code_variables_mask_patterns_context(["mask-me"])
        set_code_variables_ignore_patterns_context(["ignore-me"])
        set_code_variables_mask_url_credentials_context(False)
        set_code_variables_detect_secrets_context(False)
        try:
            raise ValueError("boom")
        except ValueError as error:
            assert client.capture_exception(error, distinct_id="user-1") is not None

    attach.assert_called_once()
    assert attach.call_args.kwargs == {
        "mask_patterns": ["mask-me"],
        "ignore_patterns": ["ignore-me"],
        "mask_url_credentials": False,
        "detect_secrets": False,
    }
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
async def test_capture_immediate_offloads_synchronous_on_error():
    callback_thread = None

    def on_error(error, batch):
        nonlocal callback_thread
        callback_thread = threading.get_ident()

    with mock.patch(
        "posthog._async_consumer.async_batch_post",
        side_effect=APIError(400, "failed"),
    ):
        client = AsyncPosthog("test-key", on_error=on_error, max_retries=0)
        result = await client.capture_immediate("event", distinct_id="user-1")
        await client.shutdown()

    assert result is None
    assert callback_thread != threading.get_ident()


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name,method_kwargs,expected_id,expected_properties",
    [
        (
            "group_identify",
            {
                "group_type": "company",
                "group_key": "company-1",
                "properties": {"plan": "pro", "nested": {"drop": None, "count": 0}},
                "distinct_id": "group-user",
            },
            "group-user",
            {
                "$group_type": "company",
                "$group_key": "company-1",
                "$group_set": {"plan": "pro", "nested": {"drop": None, "count": 0}},
            },
        ),
        (
            "alias",
            {"previous_id": 123, "distinct_id": "alias-user"},
            "123",
            {"distinct_id": "123", "alias": "alias-user"},
        ),
    ],
)
async def test_identity_events_preserve_payload_session_and_normalized_envelope(
    method_name, method_kwargs, expected_id, expected_properties
):
    delivered = []

    async def batch_post(*args, **kwargs):
        delivered.extend(kwargs["batch"])

    with mock.patch("posthog._async_consumer.async_batch_post", side_effect=batch_post):
        async with AsyncPosthog("test-key", flush_interval=30) as client:
            with new_context(fresh=True):
                set_context_session("request-session")
                event_uuid = getattr(client, method_name)(
                    **method_kwargs,
                    uuid="{4BF92F35-77B3-4DA6-A3CE-929D0E0E4736}",
                    timestamp="2025-01-02T03:04:05.123456+02:30",
                    disable_geoip=False,
                )
            await client.flush(timeout_seconds=1)

    assert len(delivered) == 1
    event = delivered[0]
    assert event_uuid == event["uuid"] == "4bf92f35-77b3-4da6-a3ce-929d0e0e4736"
    assert event["timestamp"] == "2025-01-02T00:34:05.123456+00:00"
    assert event["distinct_id"] == expected_id
    assert {
        key: event["properties"][key] for key in expected_properties
    } == expected_properties
    assert event["properties"]["$session_id"] == "request-session"
    assert "$geoip_disable" not in event["properties"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name,method_kwargs",
    [
        ("group_identify", {"group_type": "", "group_key": "company-1"}),
        ("group_identify", {"group_type": "company", "group_key": ""}),
        ("alias", {"previous_id": "", "distinct_id": "user-1"}),
        ("alias", {"previous_id": "anon-1", "distinct_id": None}),
    ],
)
async def test_identity_events_reject_missing_required_ids(method_name, method_kwargs):
    with (
        new_context(fresh=True),
        mock.patch(
            "posthog._async_consumer.async_batch_post", new=mock.AsyncMock()
        ) as send,
    ):
        async with AsyncPosthog("test-key") as client:
            assert getattr(client, method_name)(**method_kwargs) is None
            await client.flush()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_alias_uses_request_identity_when_no_explicit_distinct_id_is_supplied():
    delivered = []

    async def batch_post(*args, **kwargs):
        delivered.extend(kwargs["batch"])

    with mock.patch("posthog._async_consumer.async_batch_post", side_effect=batch_post):
        async with AsyncPosthog("test-key") as client:
            with new_context(fresh=True):
                identify_context("request-user")
                assert client.alias("anon-1", None) is not None
            await client.flush()

    assert delivered[0]["distinct_id"] == "anon-1"
    assert delivered[0]["properties"]["alias"] == "request-user"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_immediate", [False, True])
async def test_concurrent_shutdown_closes_once_after_immediate_operation_finishes(
    cancel_immediate,
):
    started, release = asyncio.Event(), asyncio.Event()
    http_client = mock.Mock(aclose=mock.AsyncMock())
    on_error = mock.AsyncMock()

    async def batch_post(*args, **kwargs):
        started.set()
        await release.wait()

    with (
        mock.patch("posthog.async_client._build_client", return_value=http_client),
        mock.patch("posthog._async_consumer.async_batch_post", side_effect=batch_post),
    ):
        client = AsyncPosthog("test-key", on_error=on_error)
        capture = asyncio.create_task(client.capture_immediate("in flight"))
        await started.wait()
        shutdowns = [asyncio.create_task(client.shutdown()) for _ in range(2)]
        await asyncio.sleep(0)
        assert client.capture("too late") is None
        assert not any(task.done() for task in shutdowns)
        http_client.aclose.assert_not_awaited()
        if cancel_immediate:
            capture.cancel()
            with pytest.raises(asyncio.CancelledError):
                await capture
        else:
            release.set()
            assert await capture is not None
        await asyncio.wait_for(asyncio.gather(*shutdowns), timeout=1)

    http_client.aclose.assert_awaited_once_with()
    on_error.assert_not_awaited()
    assert client._immediate_callers == {}
    assert client._inflight_operations == set()
    assert await client.capture_immediate("after shutdown") is None


@pytest.mark.asyncio
async def test_cancelled_shutdown_can_be_retried_without_cancelling_buffered_delivery():
    started, release = asyncio.Event(), asyncio.Event()
    delivered = []

    async def batch_post(*args, **kwargs):
        started.set()
        await release.wait()
        delivered.extend(kwargs["batch"])

    with mock.patch("posthog._async_consumer.async_batch_post", side_effect=batch_post):
        client = AsyncPosthog("test-key", flush_at=1)
        event_uuid = client.capture("buffered")
        await started.wait()
        shutdown = asyncio.create_task(client.shutdown())
        await asyncio.sleep(0)
        shutdown.cancel()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        assert client.capture("after cancelled shutdown") is None
        release.set()
        await asyncio.wait_for(client.shutdown(), timeout=1)

    assert [event["uuid"] for event in delivered] == [event_uuid]


@pytest.mark.asyncio
@pytest.mark.parametrize("debug", [False, True])
async def test_shutdown_attempts_remaining_cleanup_before_reporting_first_error(debug):
    first_error = RuntimeError("flush failed")
    http_client = mock.Mock(
        aclose=mock.AsyncMock(side_effect=ValueError("close failed"))
    )
    with (
        mock.patch("posthog.async_client._build_client", return_value=http_client),
        mock.patch(
            "posthog.async_client._async_flags", new=mock.AsyncMock(return_value={})
        ),
    ):
        client = AsyncPosthog("test-key", debug=debug, secret_key="secret-key")
        await client.evaluate_flags("user-1")
        emails = client.emails
        with (
            mock.patch.object(
                client, "flush", new=mock.AsyncMock(side_effect=first_error)
            ),
            mock.patch.object(
                emails,
                "aclose",
                new=mock.AsyncMock(side_effect=OSError("email close failed")),
            ) as close_email,
        ):
            if debug:
                with pytest.raises(RuntimeError) as raised:
                    await client.shutdown()
                assert raised.value is first_error
            else:
                await client.shutdown()
            await client.shutdown()

    http_client.aclose.assert_awaited_once_with()
    close_email.assert_awaited_once_with()
    assert client._duplicate_client_registry_key is None
    assert client._closed
    assert client.capture("after shutdown") is None


@pytest.mark.asyncio
async def test_immediate_capture_replaces_invalid_uuid_and_timestamp_before_hook():
    observed = []

    async def before_send(event):
        observed.append(event.copy())
        return {**event, "uuid": "hook-cannot-replace-event-identity"}

    with mock.patch(
        "posthog._async_consumer.async_batch_post", new=mock.AsyncMock()
    ) as send:
        client = AsyncPosthog("test-key", before_send=before_send)
        before = datetime.now(timezone.utc)
        event_uuid = await client.capture_immediate(
            "event", uuid="invalid", timestamp="not-a-date"
        )
        after = datetime.now(timezone.utc)
        await client.shutdown()

    assert str(UUID(event_uuid)) == event_uuid
    assert observed[0]["uuid"] == event_uuid
    assert before <= datetime.fromisoformat(observed[0]["timestamp"]) <= after
    assert send.await_args.kwargs["batch"][0]["uuid"] == event_uuid


@pytest.mark.asyncio
async def test_deprecated_secret_key_warning_points_to_constructor_caller():
    with pytest.warns(DeprecationWarning, match="personal_api_key") as warnings_seen:
        client = AsyncPosthog("test-key", personal_api_key=" legacy-key ", send=False)
    assert Path(warnings_seen[0].filename) == Path(__file__)
    assert client.secret_key == client.personal_api_key == "legacy-key"
    await client.shutdown()
