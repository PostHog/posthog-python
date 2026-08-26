from __future__ import annotations

import asyncio
import logging
from unittest import mock

import pytest

from posthog import AsyncPosthog
from posthog.client import _MINIMAL_FLAG_CALLED_EVENT_PROPERTIES
from posthog.contexts import new_context, set_context_device_id
from posthog.request import APIError


def flags_response(*, minimal=False):
    return {
        "flags": {
            "beta": {
                "enabled": True,
                "variant": "control",
                "reason": {"description": "Matched rollout"},
                "metadata": {
                    "id": 123,
                    "version": 4,
                    "payload": '{"color": "blue"}',
                    "has_experiment": False,
                },
            },
            "disabled": {
                "enabled": False,
                "variant": None,
                "metadata": {
                    "id": 456,
                    "version": 2,
                    "payload": None,
                    "has_experiment": True,
                },
            },
        },
        "requestId": "request-1",
        "evaluatedAt": 123456,
        "errorsWhileComputingFlags": False,
        "minimalFlagCalledEvents": minimal,
    }


@pytest.mark.asyncio
async def test_evaluate_flags_uses_remote_async_request_and_returns_snapshot():
    with mock.patch(
        "posthog.async_client._async_flags",
        new=mock.AsyncMock(return_value=flags_response()),
    ) as async_flags:
        client = AsyncPosthog("project-key", send=False)
        snapshot = await client.evaluate_flags(
            "user-1",
            groups={"company": "company-1"},
            person_properties={"plan": "pro"},
            group_properties={"company": {"size": 10}},
            disable_geoip=False,
            flag_keys=["beta", "disabled"],
            device_id="device-1",
        )
        await client.shutdown()

    assert snapshot.keys == ["beta", "disabled"]
    assert snapshot.get_flag("beta") == "control"
    assert snapshot.is_enabled("disabled", default_value=True) is False
    assert snapshot.get_flag_payload("beta") == {"color": "blue"}
    async_flags.assert_awaited_once()
    kwargs = async_flags.await_args.kwargs
    assert kwargs["groups"] == {"company": "company-1"}
    assert kwargs["person_properties"] == {"plan": "pro"}
    assert kwargs["group_properties"] == {"company": {"size": 10}}
    assert kwargs["geoip_disable"] is False
    assert kwargs["flag_keys_to_evaluate"] == ["beta", "disabled"]
    assert kwargs["device_id"] == "device-1"


@pytest.mark.asyncio
async def test_evaluate_flags_uses_context_device_id():
    with mock.patch(
        "posthog.async_client._async_flags",
        new=mock.AsyncMock(return_value=flags_response()),
    ) as async_flags:
        client = AsyncPosthog("project-key", send=False)
        with new_context(fresh=True):
            set_context_device_id("context-device")
            await client.evaluate_flags("user-1")
        await client.shutdown()

    assert async_flags.await_args.kwargs["device_id"] == "context-device"


@pytest.mark.asyncio
@pytest.mark.parametrize("distinct_id", [None, ""])
async def test_evaluate_flags_without_distinct_id_returns_empty_snapshot(distinct_id):
    with mock.patch(
        "posthog.async_client._async_flags", new=mock.AsyncMock()
    ) as async_flags:
        client = AsyncPosthog("project-key", send=False)
        snapshot = await client.evaluate_flags(distinct_id)
        await client.shutdown()

    assert snapshot.keys == []
    assert snapshot.is_enabled("missing") is False
    async_flags.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_flag_keys_skips_remote_request():
    with mock.patch(
        "posthog.async_client._async_flags", new=mock.AsyncMock()
    ) as async_flags:
        client = AsyncPosthog("project-key", send=False)
        snapshot = await client.evaluate_flags("user-1", flag_keys=[])
        await client.shutdown()

    assert snapshot.keys == []
    async_flags.assert_not_awaited()


@pytest.mark.asyncio
async def test_snapshot_access_captures_one_flag_called_event_and_attaches_flags():
    batches = []

    async def batch_post(*args, **kwargs):
        batches.append(kwargs["batch"])

    with (
        mock.patch(
            "posthog.async_client._async_flags",
            new=mock.AsyncMock(return_value=flags_response()),
        ),
        mock.patch("posthog._async_consumer.async_batch_post", side_effect=batch_post),
    ):
        client = AsyncPosthog("project-key", flush_interval=30)
        snapshot = await client.evaluate_flags("user-1")
        assert snapshot.is_enabled("beta") is True
        assert snapshot.is_enabled("beta") is True
        client.capture("business event", distinct_id="user-1", flags=snapshot)
        await client.flush(timeout_seconds=1)
        await client.shutdown()

    events = [event for batch in batches for event in batch]
    called = [event for event in events if event["event"] == "$feature_flag_called"]
    assert len(called) == 1
    assert called[0]["properties"]["$feature_flag_request_id"] == "request-1"
    assert called[0]["properties"]["$feature_flag_response"] == "control"
    business = next(event for event in events if event["event"] == "business event")
    assert business["properties"]["$feature/beta"] == "control"
    assert business["properties"]["$feature/disabled"] is False
    assert business["properties"]["$active_feature_flags"] == ["beta"]


@pytest.mark.asyncio
async def test_minimal_flag_called_event_uses_strict_property_allowlist():
    batches = []

    async def batch_post(*args, **kwargs):
        batches.append(kwargs["batch"])

    with (
        mock.patch(
            "posthog.async_client._async_flags",
            new=mock.AsyncMock(return_value=flags_response(minimal=True)),
        ),
        mock.patch("posthog._async_consumer.async_batch_post", side_effect=batch_post),
    ):
        client = AsyncPosthog("project-key", flush_interval=30)
        snapshot = await client.evaluate_flags("user-1", flag_keys=["beta"])
        snapshot.get_flag("beta")
        await client.flush(timeout_seconds=1)
        await client.shutdown()

    event = next(
        event
        for batch in batches
        for event in batch
        if event["event"] == "$feature_flag_called"
    )
    assert set(event["properties"]) <= _MINIMAL_FLAG_CALLED_EVENT_PROPERTIES
    assert {
        "$feature_flag",
        "$feature_flag_response",
        "$feature_flag_request_id",
        "$feature_flag_has_experiment",
    } <= set(event["properties"])


@pytest.mark.asyncio
async def test_evaluate_flags_failure_is_sanitized_and_returns_empty_snapshot(caplog):
    caplog.set_level(logging.DEBUG, logger="posthog")
    error = APIError(400, "password=server-secret")

    with mock.patch("posthog.async_client._async_flags", side_effect=error):
        client = AsyncPosthog("project-key", send=False)
        snapshot = await client.evaluate_flags("user-1")
        await client.shutdown()

    assert snapshot.keys == []
    assert "server-secret" not in caplog.text
    assert "APIError" in caplog.text
    assert "status=400" in caplog.text


@pytest.mark.asyncio
async def test_shutdown_waits_for_in_flight_flag_evaluation():
    request_started = asyncio.Event()
    allow_response = asyncio.Event()
    http_client = mock.Mock()
    http_client.aclose = mock.AsyncMock()

    async def async_flags(*args, **kwargs):
        request_started.set()
        await allow_response.wait()
        return flags_response()

    with (
        mock.patch("posthog.async_client._build_client", return_value=http_client),
        mock.patch("posthog.async_client._async_flags", side_effect=async_flags),
    ):
        client = AsyncPosthog("project-key", send=False)
        evaluation = asyncio.create_task(client.evaluate_flags("user-1"))
        await request_started.wait()
        shutdown = asyncio.create_task(client.shutdown())
        await asyncio.sleep(0)
        http_client.aclose.assert_not_awaited()
        allow_response.set()
        snapshot = await evaluation
        await shutdown

    assert snapshot.get_flag("beta") == "control"
    http_client.aclose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_remote_config_requires_secret_key():
    with mock.patch(
        "posthog.async_client._async_remote_config", new=mock.AsyncMock()
    ) as remote_config:
        client = AsyncPosthog("project-key", send=False)
        assert await client.get_remote_config_payload("flag") is None
        await client.shutdown()

    remote_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_config_uses_secret_key_and_owned_transport():
    http_client = mock.Mock()
    http_client.aclose = mock.AsyncMock()

    with (
        mock.patch("posthog.async_client._build_client", return_value=http_client),
        mock.patch(
            "posthog.async_client._async_remote_config",
            new=mock.AsyncMock(return_value={"color": "blue"}),
        ) as remote_config,
    ):
        client = AsyncPosthog("project-key", secret_key="secret-key", send=False)
        result = await client.get_remote_config_payload("flag")
        await client.shutdown()

    assert result == {"color": "blue"}
    remote_config.assert_awaited_once_with(
        "secret-key",
        "project-key",
        client.host,
        "flag",
        timeout=3,
        client=http_client,
    )
    http_client.aclose.assert_awaited_once_with()


def test_deprecated_single_flag_methods_are_not_added_to_async_client():
    client = AsyncPosthog("project-key", send=False)
    assert not hasattr(client, "get_feature_flag")
    assert not hasattr(client, "get_feature_flag_payload")
    assert not hasattr(client, "feature_enabled")
