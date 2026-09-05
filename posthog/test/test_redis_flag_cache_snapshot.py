"""Shared Redis results must retain the definitions that produced them."""

import json
import threading
from copy import deepcopy
from unittest import mock

import pytest

from posthog.client import Client
from posthog.request import APIError, GetResponse
from posthog.test.test_flag_definition_cache import (
    AsyncMockCacheProvider,
    MockCacheProvider,
)
from posthog.test.test_property_matching_version import definitions
from posthog.test.test_utils import FAKE_TEST_API_KEY, FakeRedis
from posthog.types import FeatureFlag, FeatureFlagResult
from posthog.utils import RedisFlagCache


@pytest.fixture
def workers():
    redis = FakeRedis()
    clients = []

    def worker():
        with mock.patch.object(
            Client, "_initialize_flag_cache", return_value=RedisFlagCache(redis)
        ):
            client = Client(
                FAKE_TEST_API_KEY,
                secret_key="test-secret",
                send=False,
                enable_local_evaluation=False,
            )
        clients.append(client)
        return client

    yield worker
    for client in clients:
        client.shutdown()


def load(client, data):
    with mock.patch(
        "posthog.client.get",
        return_value=GetResponse(data=data, etag=None, not_modified=False),
    ):
        client._load_feature_flags()


def evaluate(client, key="person"):
    return client.get_feature_flag_result(
        key,
        "user",
        person_properties={"value": "banana"},
        groups={"company": "company-id"},
        group_properties={"company": {"value": "banana"}},
        only_evaluate_locally=True,
        send_feature_flag_events=False,
    )


def fallback(client, key="person"):
    with mock.patch.object(
        client,
        "_get_feature_flag_details_from_server",
        side_effect=APIError(503, "offline"),
    ) as remote:
        result = client.get_feature_flag_result(
            key, "user", send_feature_flag_events=False
        )
    remote.assert_called_once()
    return result


def changed_definitions(change):
    # The shared fixture reuses property dicts across flags and cohorts. A wire
    # round trip makes these independent so cohort-only changes really are isolated.
    data = json.loads(json.dumps(definitions(1)))
    if change == "version":
        data["property_matching_version"] = 2
    elif change == "flags":
        data["flags"][0]["filters"]["groups"][0]["properties"][0]["value"] = True
    elif change == "cohorts":
        data["cohorts"]["2"]["values"][0]["value"] = True
    else:
        data["group_type_mapping"] = {"0": "organization"}
    return data


@pytest.mark.parametrize("change", ["flags", "version", "cohorts", "mapping"])
@pytest.mark.parametrize(
    "provider_class", [None, MockCacheProvider, AsyncMockCacheProvider]
)
def test_invalidated_result_does_not_revive_after_restart(
    workers, change, provider_class
):
    writer = workers()
    load(writer, definitions(1))
    assert evaluate(writer).get_value() is True
    updated = changed_definitions(change)
    load(writer, updated)
    assert writer.flag_cache.get_stale_cached_flag("user", "person") is None
    writer.shutdown()

    reader = workers()
    if provider_class is None:
        load(reader, updated)
    else:
        provider = provider_class()
        provider.should_fetch_return_value = False
        provider.stored_data = updated
        reader._flag_definition_cache_provider = provider
        reader._load_feature_flags()
    assert (
        reader.flag_cache.get_cached_flag(
            "user", "person", reader.flag_definition_version
        )
        is None
    )
    assert fallback(reader) is None


@pytest.mark.parametrize("change", ["flags", "version", "cohorts", "mapping"])
def test_old_worker_write_is_not_accepted_by_current_worker(workers, change):
    old = workers()
    current = workers()
    load(old, definitions(1))
    load(current, changed_definitions(change))
    assert old.flag_definition_version == current.flag_definition_version
    assert evaluate(old).get_value() is True
    assert (
        current.flag_cache.get_cached_flag(
            "user", "person", current.flag_definition_version
        )
        is None
    )
    assert fallback(current) is None


@pytest.mark.parametrize("change", ["cohorts", "mapping"])
def test_refresh_during_evaluation_cannot_relabel_result(workers, change):
    client = workers()
    load(client, definitions(1))
    from posthog import client as client_module

    original = client_module.match_feature_flag_properties

    def refresh(*args, **kwargs):
        load(client, changed_definitions(change))
        return original(*args, **kwargs)

    with mock.patch(
        "posthog.client.match_feature_flag_properties", side_effect=refresh
    ):
        assert evaluate(client).get_value() is True
    assert fallback(client) is None


def test_paused_setex_keeps_original_fingerprint_after_refresh_and_restart(workers):
    writer = workers()
    load(writer, definitions(1))
    entered = threading.Event()
    release = threading.Event()
    errors = []
    original = writer.flag_cache.redis.setex

    def pause(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    def write():
        try:
            assert evaluate(writer).get_value() is True
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=write)
    with mock.patch.object(writer.flag_cache.redis, "setex", side_effect=pause):
        try:
            thread.start()
            assert entered.wait(2)
            load(writer, definitions(2))
        finally:
            release.set()
            thread.join(5)
    assert not thread.is_alive() and not errors
    reader = workers()
    load(reader, definitions(2))
    assert fallback(reader) is None


def test_legacy_missing_metadata_is_miss_but_standalone_cache_still_reads(workers):
    client = workers()
    load(client, definitions(1))
    cache = client.flag_cache
    standalone = RedisFlagCache(cache.redis)
    standalone.set_cached_flag("user", "person", True, client.flag_definition_version)
    assert (
        standalone.get_cached_flag("user", "person", client.flag_definition_version)
        is True
    )
    assert standalone.get_stale_cached_flag("user", "person") is True
    assert (
        cache.get_cached_flag("user", "person", client.flag_definition_version) is None
    )
    assert fallback(client) is None
    assert evaluate(client).get_value() is True
    # Additive metadata remains readable without opting into snapshot validation.
    assert standalone.get_stale_cached_flag("user", "person").get_value() is True


def test_matching_snapshot_reused_despite_different_counters_and_key_order(workers):
    writer = workers()
    load(writer, definitions(2))
    load(writer, definitions(1))
    assert evaluate(writer).get_value() is True
    reader = workers()
    reordered = json.loads(json.dumps(definitions(1), sort_keys=True))
    reordered.pop("property_matching_version")  # Missing and 1 have the same default.
    load(reader, reordered)
    assert writer.flag_definition_version != reader.flag_definition_version
    assert (
        reader.flag_cache.get_cached_flag(
            "user", "person", reader.flag_definition_version
        ).get_value()
        is True
    )
    assert fallback(reader).get_value() is True
    generation = reader.flag_definition_version
    load(reader, deepcopy(reordered))
    assert reader.flag_definition_version == generation
    assert fallback(reader).get_value() is True


def test_fork_retains_snapshot_binding(workers):
    client = workers()
    load(client, definitions(2))
    assert evaluate(client).get_value() is False
    redis = client.flag_cache.redis
    with mock.patch.object(
        client, "_initialize_flag_cache", return_value=RedisFlagCache(redis)
    ):
        client._reinit_after_fork()
    assert fallback(client).get_value() is False
    old = workers()
    load(old, definitions(1))
    assert evaluate(old).get_value() is True
    assert fallback(client) is None


def remote_success(client, during_request=None):
    details = FeatureFlag.from_json({"key": "person", "enabled": True})

    def request(*args, **kwargs):
        if during_request:
            during_request()
        return details, None, None, False, False

    with mock.patch.object(
        client, "_get_feature_flag_details_from_server", side_effect=request
    ):
        return client.get_feature_flag_result(
            "person", "user", send_feature_flag_events=False
        )


def test_remote_only_success_remains_available_for_stale_fallback(workers):
    writer = workers()
    # No local definitions were available from the server.
    with mock.patch("posthog.client.get", side_effect=APIError(503, "offline")):
        assert remote_success(writer).get_value() is True
        assert fallback(writer).get_value() is True
        assert fallback(workers()).get_value() is True


@pytest.mark.parametrize("transition", ["hydrate", "empty-hydrate", 401, 402])
def test_delayed_remote_response_cannot_cross_snapshot_transition(workers, transition):
    client = workers()
    if isinstance(transition, int):
        load(client, definitions(1))

    def change():
        if isinstance(transition, int):
            with mock.patch(
                "posthog.client.get", side_effect=APIError(transition, "reset")
            ):
                client._load_feature_flags()
        else:
            data = definitions(1)
            if transition == "empty-hydrate":
                data["flags"] = []
            load(client, data)

    with mock.patch("posthog.client.get", side_effect=APIError(503, "offline")):
        assert remote_success(client, change).get_value() is True
        assert fallback(client) is None
    load(client, definitions(1))
    assert evaluate(client).get_value() is True
    assert fallback(client).get_value() is True


@pytest.mark.parametrize("status", [401, 402])
def test_reset_empty_fingerprint_cannot_cache_or_read_results(workers, status):
    client = workers()
    load(client, definitions(1))
    with mock.patch("posthog.client.get", side_effect=APIError(status, "reset")):
        client._load_feature_flags()
    assert remote_success(client).get_value() is True
    cache = client.flag_cache
    cache.redis.setex(
        cache._get_cache_key("user", "person"),
        cache.stale_ttl,
        cache._serialize_entry(
            FeatureFlagResult.from_value_and_payload("person", True, None),
            client.flag_definition_version,
            fingerprint="",
        ),
    )
    assert (
        cache.get_cached_flag("user", "person", client.flag_definition_version) is None
    )
    assert fallback(client) is None
