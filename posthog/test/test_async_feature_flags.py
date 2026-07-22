from __future__ import annotations

import logging
from unittest import mock

import pytest

from posthog import AsyncPostHog
from posthog.feature_flag_evaluations import FeatureFlagEvaluations
from posthog.request import GetResponse


def _flags_response_fixture():
    return {
        "flags": {
            "variant-flag": {
                "key": "variant-flag",
                "enabled": True,
                "variant": "variant-value",
                "reason": {"code": "variant", "description": "Matched condition set 3"},
                "metadata": {"id": 2, "version": 23, "payload": '{"key": "value"}'},
            },
            "boolean-flag": {
                "key": "boolean-flag",
                "enabled": True,
                "variant": None,
                "reason": {"code": "boolean", "description": "Matched condition set 1"},
                "metadata": {"id": 1, "version": 12},
            },
            "disabled-flag": {
                "key": "disabled-flag",
                "enabled": False,
                "variant": None,
                "reason": {
                    "code": "boolean",
                    "description": "Did not match any condition",
                },
                "metadata": {"id": 3, "version": 2},
            },
        },
        "requestId": "request-id-1",
        "evaluatedAt": 1640995200000,
    }


@pytest.mark.asyncio
async def test_evaluate_flags_uses_async_flags_endpoint():
    async def mock_flags(*args, **kwargs):
        return _flags_response_fixture()

    with mock.patch(
        "posthog.async_client._async_flags", side_effect=mock_flags
    ) as patch_flags:
        client = AsyncPostHog("test-key", send=False)
        flags = await client.evaluate_flags("user-1", flag_keys=["boolean-flag"])
        await client.shutdown()

    assert isinstance(flags, FeatureFlagEvaluations)
    assert flags.is_enabled("boolean-flag") is True
    assert patch_flags.call_args.kwargs["flag_keys_to_evaluate"] == ["boolean-flag"]


@pytest.mark.asyncio
async def test_flags_response_updates_minimal_flag_called_events_gate():
    response = {**_flags_response_fixture(), "minimalFlagCalledEvents": True}

    with mock.patch(
        "posthog.async_client._async_flags", new=mock.AsyncMock(return_value=response)
    ):
        client = AsyncPostHog("test-key", send=False)
        await client.evaluate_flags("user-1")
        assert client._minimal_flag_called_events is True
        await client.shutdown()


@pytest.mark.asyncio
async def test_feature_flag_access_schedules_feature_flag_called_capture():
    batches = []

    async def mock_flags(*args, **kwargs):
        return _flags_response_fixture()

    async def mock_batch_post(*args, **kwargs):
        batches.append(kwargs["batch"])

    with (
        mock.patch("posthog.async_client._async_flags", side_effect=mock_flags),
        mock.patch(
            "posthog._async_consumer.async_batch_post", side_effect=mock_batch_post
        ),
    ):
        async with AsyncPostHog("test-key", flush_at=1, flush_interval=0.01) as client:
            flags = await client.evaluate_flags("user-1")
            assert flags.get_flag("variant-flag") == "variant-value"
            await client.flush(timeout_seconds=1)

    assert len(batches) == 1
    event = batches[0][0]
    assert event["event"] == "$feature_flag_called"
    assert event["distinct_id"] == "user-1"
    assert event["properties"]["$feature_flag"] == "variant-flag"
    assert event["properties"]["$feature_flag_response"] == "variant-value"
    assert event["properties"]["$feature_flag_request_id"] == "request-id-1"


@pytest.mark.asyncio
async def test_get_all_flags_and_payloads_uses_async_remote_fallback():
    async def mock_flags(*args, **kwargs):
        return _flags_response_fixture()

    with mock.patch("posthog.async_client._async_flags", side_effect=mock_flags):
        client = AsyncPostHog("test-key", send=False)
        result = await client.get_all_flags_and_payloads("user-1")
        await client.shutdown()

    assert result == {
        "featureFlags": {
            "variant-flag": "variant-value",
            "boolean-flag": True,
            "disabled-flag": False,
        },
        "featureFlagPayloads": {"variant-flag": '{"key": "value"}'},
    }


@pytest.mark.asyncio
async def test_get_feature_flag_result_uses_async_remote_evaluation_and_captures_event():
    batches = []

    async def mock_flags(*args, **kwargs):
        return _flags_response_fixture()

    async def mock_batch_post(*args, **kwargs):
        batches.append(kwargs["batch"])

    with (
        mock.patch("posthog.async_client._async_flags", side_effect=mock_flags),
        mock.patch(
            "posthog._async_consumer.async_batch_post", side_effect=mock_batch_post
        ),
    ):
        async with AsyncPostHog("test-key", flush_at=1, flush_interval=0.01) as client:
            result = await client.get_feature_flag_result("boolean-flag", "user-1")
            await client.flush(timeout_seconds=1)

    assert result is not None
    assert result.get_value() is True
    assert batches[0][0]["event"] == "$feature_flag_called"
    assert batches[0][0]["properties"]["$feature_flag"] == "boolean-flag"


@pytest.mark.asyncio
async def test_load_feature_flags_uses_async_get_and_starts_async_poller():
    async def mock_get(*args, **kwargs):
        return GetResponse(
            data={"flags": [], "group_type_mapping": {}, "cohorts": {}},
            etag='"etag"',
            not_modified=False,
        )

    with mock.patch(
        "posthog.async_client._async_get", side_effect=mock_get
    ) as patch_get:
        client = AsyncPostHog(
            "test-key",
            personal_api_key="personal-key",
            poll_interval=60,
            send=False,
        )
        await client.load_feature_flags()
        assert client._flag_poll_task is not None
        await client.shutdown()

    assert patch_get.call_count == 1
    assert client.feature_flags == []
    assert client._flags_etag == '"etag"'


@pytest.mark.asyncio
async def test_load_feature_flags_writes_minimization_gate_to_external_cache():
    provider = mock.Mock()
    provider.should_fetch_flag_definitions = mock.AsyncMock(return_value=True)
    provider.get_flag_definitions = mock.AsyncMock(return_value=None)
    provider.on_flag_definitions_received = mock.AsyncMock()
    provider.shutdown = mock.AsyncMock()
    response_data = {
        "flags": [],
        "group_type_mapping": {},
        "cohorts": {},
        "minimal_flag_called_events": True,
    }

    with mock.patch(
        "posthog.async_client._async_get",
        new=mock.AsyncMock(
            return_value=GetResponse(
                data=response_data, etag='"etag"', not_modified=False
            )
        ),
    ):
        client = AsyncPostHog(
            "test-key",
            personal_api_key="personal-key",
            flag_definition_cache_provider=provider,
            poll_interval=60,
            send=False,
        )
        await client.load_feature_flags()
        await client.shutdown()

    provider.on_flag_definitions_received.assert_awaited_once_with(response_data)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disabled", "personal_api_key", "remote_result", "expected"),
    [
        (True, "personal-key", "payload", None),
        (False, None, "payload", None),
        (False, "personal-key", {"enabled": True}, {"enabled": True}),
        (False, "personal-key", RuntimeError("boom"), None),
    ],
)
async def test_get_remote_config_payload_handles_async_paths(
    disabled, personal_api_key, remote_result, expected
):
    async def mock_remote_config(*args, **kwargs):
        if isinstance(remote_result, Exception):
            raise remote_result
        return remote_result

    with mock.patch(
        "posthog.async_client._async_remote_config", side_effect=mock_remote_config
    ) as patch_remote_config:
        client = AsyncPostHog(
            "test-key",
            personal_api_key=personal_api_key,
            disabled=disabled,
            send=False,
        )
        result = await client.get_remote_config_payload("config-key")
        await client.shutdown()

    assert result == expected
    if disabled or personal_api_key is None:
        patch_remote_config.assert_not_called()
    else:
        patch_remote_config.assert_called_once()


def test_feature_flag_called_scheduling_without_running_loop_warns(caplog):
    caplog.set_level(logging.WARNING, logger="posthog")
    client = AsyncPostHog("test-key", send=False)

    client._schedule_feature_flag_called_event(
        distinct_id="user-1",
        key="flag-key",
        response=True,
        properties={"$feature_flag": "flag-key"},
    )

    assert "no event loop is running" in caplog.text
