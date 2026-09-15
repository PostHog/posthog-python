"""Versioned property matching and definitions snapshot regression tests."""

from copy import deepcopy
from unittest import mock

import pytest

from posthog.client import Client
from posthog.feature_flags import InconclusiveMatchError, match_property
from posthog.request import APIError, GetResponse
from posthog.test.test_flag_definition_cache import (
    AsyncMockCacheProvider,
    MockCacheProvider,
)
from posthog.test.test_utils import FAKE_TEST_API_KEY


ROWS = [
    (False, "banana", True, False),
    (False, 0, True, False),
    (["true", "false"], "true", False, True),
    (["true", "false"], "pro", True, False),
    ([], True, True, True),
    ([], [], True, True),
    (True, [True], True, False),
    (False, "FALSE", True, True),
    (False, None, True, False),
    (False, "", True, False),
    ([], [True, ["TRUE", []]], True, True),
    ([], [True, [False]], False, False),
    ([], False, False, False),
    ([], None, False, False),
    ([], 0, False, False),
    ([], 1, False, False),
    ([], "banana", False, False),
    ([False, "PRO"], "pro", True, True),
    ([[True], "PRO"], [True], True, True),
    ([None, "PRO"], "null", True, True),
    ([1, "PRO"], "1", True, True),
    ("οδος", "ΟΔΟΣ", True, True),
    ("i\u0307", "İ", True, True),
    ("ss", "ß", False, False),
]


@pytest.mark.parametrize("filter_value,property_value,legacy,explicit", ROWS)
@pytest.mark.parametrize("version", [None, 1, 2, 0, 3, "2"])
@pytest.mark.parametrize("operator", ["exact", "is_not"])
def test_match_property_version_rows(
    filter_value, property_value, legacy, explicit, version, operator
):
    kwargs = {} if version is None else {"property_matching_version": version}
    expected = explicit if version == 2 else legacy
    assert match_property(
        {"key": "value", "value": filter_value, "operator": operator},
        {"value": property_value},
        **kwargs,
    ) is (expected if operator == "exact" else not expected)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("operator", ["exact", "is_not"])
def test_match_property_version_missing_is_inconclusive(version, operator):
    with pytest.raises(InconclusiveMatchError):
        match_property(
            {"key": "value", "value": False, "operator": operator},
            {},
            property_matching_version=version,
        )


def definitions(version=None):
    prop = {"key": "value", "value": False, "operator": "exact"}
    person = {
        "key": "person",
        "active": True,
        "version": 2,  # Individual flag versions must not select matching semantics.
        "filters": {"groups": [{"properties": [prop]}]},
    }
    group = deepcopy(person)
    group["key"] = "group"
    group["filters"]["aggregation_group_type_index"] = 0
    mixed = deepcopy(person)
    mixed["key"] = "mixed"
    mixed["filters"]["groups"][0]["aggregation_group_type_index"] = 0
    cohort = deepcopy(person)
    cohort["key"] = "cohort"
    cohort["filters"]["groups"][0]["properties"] = [{"type": "cohort", "value": 1}]
    dependency = deepcopy(person)
    dependency["key"] = "dependency"
    dependency["filters"]["groups"][0]["properties"] = [
        {
            "type": "flag",
            "key": "person",
            "value": True,
            "operator": "flag_evaluates_to",
            "dependency_chain": ["person"],
        }
    ]
    data = {
        "flags": [person, group, mixed, cohort, dependency],
        "group_type_mapping": {"0": "company"},
        "cohorts": {
            "1": {
                "type": "AND",
                "values": [{"type": "OR", "values": [{"type": "cohort", "value": 2}]}],
            },
            "2": {"type": "AND", "values": [prop]},
        },
    }
    if version is not None:
        data["property_matching_version"] = version
    return data


@pytest.fixture
def client():
    client = Client(
        FAKE_TEST_API_KEY,
        secret_key="test-secret",
        enable_local_evaluation=False,
        send=False,
        flag_fallback_cache_url="memory://local/?ttl=300&size=100",
    )
    with mock.patch(
        "posthog.client.flags", side_effect=AssertionError("remote fallback")
    ):
        yield client
    client.shutdown()


def evaluate(client, key="person"):
    return client.get_feature_flag(
        key,
        "user",
        person_properties={"value": "banana"},
        groups={"company": "company-id"},
        group_properties={"company": {"value": "banana"}},
        only_evaluate_locally=True,
        send_feature_flag_events=False,
    )


@pytest.mark.parametrize(
    "version,expected", [(None, True), (1, True), (2, False), (3, True)]
)
def test_client_version_person_group_cohort_dependency_and_full_api(
    client, version, expected
):
    client._update_flag_state(definitions(version))
    for key in ("person", "group", "mixed", "cohort", "dependency"):
        assert evaluate(client, key) is expected
    results = client.evaluate_flags(
        "user",
        person_properties={"value": "banana"},
        groups={"company": "company-id"},
        group_properties={"company": {"value": "banana"}},
        only_evaluate_locally=True,
    )
    for key in ("person", "group", "mixed", "cohort", "dependency"):
        assert results.get_flag(key) is expected


@pytest.mark.parametrize("provider_class", [MockCacheProvider, AsyncMockCacheProvider])
def test_version_only_reload_invalidates_results_and_round_trips_provider(
    client, provider_class
):
    provider = provider_class()
    client._flag_definition_cache_provider = provider
    with mock.patch("posthog.client.get") as get:
        for version, expected in [
            (1, True),
            (2, False),
            (1, True),
            (2, False),
            (None, True),
        ]:
            previous_generation = client.flag_definition_version
            get.return_value = GetResponse(
                data=definitions(version), etag=str(version), not_modified=False
            )
            client._load_feature_flags()
            assert client.flag_definition_version == previous_generation + 1
            assert client.flag_cache.get_stale_cached_flag("user", "person") is None
            assert evaluate(client) is expected
            assert (
                client.flag_cache.get_cached_flag(
                    "user", "person", client.flag_definition_version
                ).get_value()
                is expected
            )
            assert provider.stored_data["property_matching_version"] == (version or 1)


@pytest.mark.parametrize(
    "refresh", ["304", "503", "exception", "empty-provider", "failed-provider"]
)
def test_failed_or_not_modified_refresh_preserves_version(client, refresh):
    with mock.patch("posthog.client.get") as get:
        get.return_value = GetResponse(
            data=definitions(2), etag="current", not_modified=False
        )
        client._load_feature_flags()
        generation = client.flag_definition_version
        if refresh == "304":
            get.return_value = GetResponse(data=None, etag="current", not_modified=True)
        elif refresh == "503":
            get.side_effect = APIError(503, "unavailable")
        elif refresh == "exception":
            get.side_effect = RuntimeError("offline")
        else:
            provider = MockCacheProvider()
            client._flag_definition_cache_provider = provider
            provider.should_fetch_return_value = False
            if refresh == "failed-provider":
                provider.get_error = RuntimeError("cache unavailable")
                get.side_effect = RuntimeError("offline")
        client._load_feature_flags()
        assert client.flag_definition_version == generation
        assert evaluate(client) is False


def test_in_flight_full_evaluation_keeps_matching_snapshot(client):
    client._update_flag_state(definitions(1))
    from posthog import client as client_module

    original = client_module.match_feature_flag_properties
    calls = 0

    def reload_during_evaluation(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            client._update_flag_state(definitions(2), client.feature_flags_by_key)
        return original(*args, **kwargs)

    with mock.patch(
        "posthog.client.match_feature_flag_properties",
        side_effect=reload_during_evaluation,
    ):
        result, fallback = client._get_all_flags_and_payloads_locally(
            "user",
            groups={"company": "company-id"},
            person_properties={"value": "banana"},
            group_properties={"company": {"value": "banana"}},
        )
    assert not fallback
    assert all(result["featureFlags"].values())
    assert evaluate(client) is False


@pytest.mark.parametrize("provider_class", [MockCacheProvider, AsyncMockCacheProvider])
def test_provider_hydration_version_only_changes_and_older_entries(
    client, provider_class
):
    provider = provider_class()
    provider.should_fetch_return_value = False
    client._flag_definition_cache_provider = provider
    with mock.patch(
        "posthog.client.get", side_effect=AssertionError("API fetch")
    ) as get:
        for version, expected in [
            (1, True),
            (2, False),
            (1, True),
            (2, False),
            (None, True),
        ]:
            provider.stored_data = definitions(version)
            generation = client.flag_definition_version
            client._load_feature_flags()
            assert client.flag_definition_version == generation + 1
            assert client.flag_cache.get_stale_cached_flag("user", "person") is None
            for key in ("person", "group", "mixed", "cohort", "dependency"):
                assert evaluate(client, key) is expected
        get.assert_not_called()


@pytest.mark.parametrize("provider_class", [MockCacheProvider, AsyncMockCacheProvider])
def test_provider_round_trip_to_second_worker(client, provider_class):
    provider = provider_class()
    client._flag_definition_cache_provider = provider
    with mock.patch(
        "posthog.client.get",
        return_value=GetResponse(data=definitions(2), etag="v2", not_modified=False),
    ):
        client._load_feature_flags()
    provider.should_fetch_return_value = False
    reader = Client(
        FAKE_TEST_API_KEY,
        secret_key="reader-secret",
        enable_local_evaluation=False,
        send=False,
        flag_definition_cache_provider=provider,
    )
    try:
        with mock.patch(
            "posthog.client.get", side_effect=AssertionError("API fetch")
        ) as get:
            reader._load_feature_flags()
            for key in ("person", "group", "mixed", "cohort", "dependency"):
                assert evaluate(reader, key) is False
            get.assert_not_called()
    finally:
        reader.shutdown()


@pytest.mark.parametrize("status", [401, 402])
def test_clearing_definitions_resets_matching_version(client, status):
    client._update_flag_state(definitions(2))
    assert evaluate(client) is False
    with mock.patch("posthog.client.get", side_effect=APIError(status, "reset")):
        client._load_feature_flags()
    assert client._local_evaluation_snapshot()["property_matching_version"] == 1
    assert not client.feature_flags
    assert client.flag_cache.get_stale_cached_flag("user", "person") is None
    client._update_flag_state(definitions())
    assert evaluate(client) is True


@pytest.mark.parametrize("version,expected", [(None, True), (1, True), (2, False)])
@pytest.mark.parametrize(
    "provider_class", [None, MockCacheProvider, AsyncMockCacheProvider]
)
def test_lazy_loading_caches_first_local_result_for_outage_fallback(
    client, version, expected, provider_class
):
    assert client.feature_flags is None
    assert client.flag_definition_version == 0
    if provider_class is not None:
        provider = provider_class()
        provider.should_fetch_return_value = False
        provider.stored_data = definitions(version)
        client._flag_definition_cache_provider = provider

    with mock.patch(
        "posthog.client.get",
        return_value=GetResponse(
            data=definitions(version), etag="initial", not_modified=False
        ),
    ) as get:
        assert evaluate(client) is expected
        if provider_class is None:
            get.assert_called_once()
        else:
            get.assert_not_called()
            assert provider.get_call_count == 1

    assert client.flag_definition_version == 1
    cached = client.flag_cache.get_cached_flag(
        "user", "person", client.flag_definition_version
    )
    assert cached is not None
    assert cached.get_value() is expected

    # Missing properties make local evaluation inconclusive; an API outage must
    # still be able to fall back to the first successful evaluation.
    with mock.patch.object(
        client,
        "_get_feature_flag_details_from_server",
        side_effect=APIError(503, "offline"),
    ) as remote:
        result = client.get_feature_flag_result(
            "person", "user", send_feature_flag_events=False
        )
        remote.assert_called_once()
    assert result is cached


def test_in_flight_result_is_not_cached_in_new_generation(client):
    client._update_flag_state(definitions(1))
    from posthog import client as client_module

    original = client_module.match_feature_flag_properties

    def reload_during_evaluation(*args, **kwargs):
        client._update_flag_state(definitions(2), client.feature_flags_by_key)
        return original(*args, **kwargs)

    with mock.patch(
        "posthog.client.match_feature_flag_properties",
        side_effect=reload_during_evaluation,
    ):
        assert evaluate(client) is True
    assert client.flag_cache.get_stale_cached_flag("user", "person") is None
    assert evaluate(client) is False
