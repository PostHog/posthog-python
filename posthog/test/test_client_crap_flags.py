"""Client flag initialization and failure contracts protected during decomposition."""

import sys
from unittest import mock

import pytest

from posthog.client import Client
from posthog.request import APIError
from posthog.test.test_property_matching_version import definitions
from posthog.test.test_utils import FAKE_TEST_API_KEY, FakeRedis
from posthog.utils import FlagCache, RedisFlagCache


@pytest.fixture
def client():
    instance = Client(
        FAKE_TEST_API_KEY,
        secret_key="test-secret",
        send=False,
        enable_local_evaluation=False,
    )
    instance.feature_flags = []
    yield instance
    instance.shutdown()


@pytest.mark.parametrize(
    "configured,expected,ttl",
    [
        ("redis://", "redis://localhost:6379", 300),
        ("redis://cache/2?ttl=17", "redis://cache:6379/2", 17),
        ("redis://user@cache:6380/0", "redis://user:@cache:6380/0", 300),
        ("redis://:secret@cache/0", "redis://:secret@cache:6379/0", 300),
        (
            "rediss://user:secret@[::1]:6380/3?ttl=11",
            "rediss://user:secret@[::1]:6380/3",
            11,
        ),
    ],
)
def test_redis_cache_initialization_preserves_url_and_ttl(
    monkeypatch, configured, expected, ttl
):
    redis_module = mock.Mock()
    monkeypatch.setitem(sys.modules, "redis", redis_module)
    instance = Client(
        FAKE_TEST_API_KEY,
        send=False,
        enable_local_evaluation=False,
        flag_fallback_cache_url=configured,
    )
    try:
        redis_module.from_url.assert_called_once_with(expected)
        redis_module.from_url.return_value.ping.assert_called_once_with()
        assert isinstance(instance.flag_cache, RedisFlagCache)
        assert instance.flag_cache.default_ttl == ttl
    finally:
        instance.shutdown()


@pytest.mark.parametrize("failure", ["missing_dependency", "connection_failure"])
def test_redis_cache_initialization_failure_disables_only_cache(
    monkeypatch, caplog, failure
):
    if failure == "missing_dependency":
        monkeypatch.setitem(sys.modules, "redis", None)
        expected_warning = "Redis not available"
    else:
        redis_module = mock.Mock()
        redis_module.from_url.return_value.ping.side_effect = RuntimeError("offline")
        monkeypatch.setitem(sys.modules, "redis", redis_module)
        expected_warning = "Redis connection failed: offline"
    instance = Client(
        FAKE_TEST_API_KEY,
        send=False,
        enable_local_evaluation=False,
        flag_fallback_cache_url="redis://cache/0",
    )
    try:
        assert instance.flag_cache is None
        assert not instance.disabled
        assert expected_warning in caplog.text
    finally:
        instance.shutdown()


@pytest.mark.parametrize("status", [401, 402])
@pytest.mark.parametrize("debug", [False, True])
def test_definition_error_invalidates_redis_snapshot_even_when_debug_raises(
    client, status, debug
):
    client.flag_cache = RedisFlagCache(FakeRedis())
    client._update_flag_state(definitions(2))
    fingerprint = client._flag_definition_fingerprint
    previous_version = client.flag_definition_version
    client.debug = debug
    with (
        mock.patch("posthog.client.get", side_effect=APIError(status, "rejected")),
        mock.patch.object(client.flag_cache, "_invalidate_snapshot") as invalidate,
    ):
        if debug:
            with pytest.raises(APIError) as error:
                client._load_feature_flags()
            assert error.value.status == status
        else:
            client._load_feature_flags()
        invalidate.assert_called_once_with(fingerprint)
    assert client.feature_flags == []
    assert client._property_matching_version == 1
    assert client._flags_etag is None
    assert client.flag_definition_version == previous_version + 1
    assert (
        client._flag_definition_published_generation
        == client._flag_definition_fetch_generation
        == client._flag_definition_cache_generation
    )


def test_remote_cache_write_failure_keeps_stale_value_and_received_metadata(client):
    client.flag_cache = FlagCache()
    initial = {"featureFlags": {"checkout": "control"}}
    with mock.patch("posthog.client.flags", return_value=initial):
        assert (
            client.get_feature_flag_result(
                "checkout", "user", send_feature_flag_events=False
            ).get_value()
            == "control"
        )

    response = {
        "requestId": "new-request",
        "evaluatedAt": 123,
        "flags": {
            "checkout": {
                "value": "test",
                "reason": {"description": "New assignment"},
                "metadata": {
                    "id": 7,
                    "version": 2,
                    "config_version": 2,
                    "rule_type": "experiment",
                    "rule_id": "rule-id",
                    "variant_key": "test",
                },
            }
        },
    }
    with (
        mock.patch("posthog.client.flags", return_value=response),
        mock.patch.object(
            client.flag_cache,
            "_set_cached_remote_flag",
            side_effect=RuntimeError("offline"),
        ),
        mock.patch.object(client, "capture") as capture,
    ):
        result = client.get_feature_flag_result("checkout", "user")
    assert result.get_value() == "control"
    capture.assert_called_once()
    event = capture.call_args.kwargs
    assert capture.call_args.args == ("$feature_flag_called",)
    properties = event["properties"]
    assert properties["$feature_flag_response"] == "control"
    assert properties["$feature_flag_error"] == "unknown_error"
    assert properties["$feature_flag_request_id"] == "new-request"
    assert properties["$feature_flag_evaluated_at"] == 123
    assert properties["$feature_flag_id"] == 7
    assert properties["$feature_flag_reason"] == "New assignment"
    assert "$feature_flag_rule_id" not in properties
