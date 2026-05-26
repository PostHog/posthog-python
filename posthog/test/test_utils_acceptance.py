import json
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID
from unittest import mock

from pytest_bdd import given, parsers, scenarios, then, when

from posthog import utils
from posthog.types import FeatureFlagResult

scenarios("features/utils.feature")


@dataclass
class AcceptancePayloadMetadata:
    source: str
    sample_rate: Decimal


PAYLOAD_VALUE_FACTORIES = {
    "uuid": lambda: UUID("12345678123456781234567812345678"),
    "decimal": lambda: Decimal("12.34"),
    "dataclass": lambda: AcceptancePayloadMetadata(
        source="checkout",
        sample_rate=Decimal("0.5"),
    ),
    "tuple": lambda: ("paid", "beta"),
    "bytes": lambda: b"hello",
    "unsupported": lambda: lambda: None,
}


@given(
    parsers.parse("an SDK payload value of type {value_type}"), target_fixture="payload"
)
def sdk_event_payload(value_type):
    return {
        "event": "plan upgraded",
        "properties": {"value": PAYLOAD_VALUE_FACTORIES[value_type]()},
    }


@when("the SDK cleans the event payload", target_fixture="cleaned_payload")
def clean_event_payload(payload):
    return utils.clean(payload)


@then(parsers.parse("the cleaned payload value equals {expected_json}"))
def cleaned_payload_value_equals(cleaned_payload, expected_json):
    assert cleaned_payload["properties"]["value"] == json.loads(expected_json)
    json.dumps(cleaned_payload)


@given("a cached feature flag evaluation for a user", target_fixture="flag_cache_state")
def cached_feature_flag_evaluation():
    cache = utils.FlagCache(max_size=10, default_ttl=60)
    flag_result = FeatureFlagResult.from_value_and_payload(
        "checkout-redesign",
        True,
        {"variant": "test"},
    )
    cache.set_cached_flag(
        "user-123",
        "checkout-redesign",
        flag_result,
        flag_definition_version=1,
    )
    return {"cache": cache, "flag_result": flag_result}


@when("the SDK reads the cached flag for current and newer definitions")
def read_cached_flag_versions(flag_cache_state):
    cache = flag_cache_state["cache"]
    flag_cache_state["current_result"] = cache.get_cached_flag(
        "user-123",
        "checkout-redesign",
        current_flag_version=1,
    )
    flag_cache_state["newer_result"] = cache.get_cached_flag(
        "user-123",
        "checkout-redesign",
        current_flag_version=2,
    )


@then("the current flag definition uses the cached evaluation")
def current_definition_uses_cached_evaluation(flag_cache_state):
    current_result = flag_cache_state["current_result"]
    assert current_result is flag_cache_state["flag_result"]
    assert current_result.get_value() is True
    assert current_result.payload == {"variant": "test"}


@then("the newer flag definition misses the cache")
def newer_definition_misses_cache(flag_cache_state):
    assert flag_cache_state["newer_result"] is None


@when("the old flag definition is invalidated")
def invalidate_old_flag_definition(flag_cache_state):
    flag_cache_state["cache"].invalidate_version(1)


@then("the cached evaluation is removed")
def cached_evaluation_is_removed(flag_cache_state):
    assert (
        flag_cache_state["cache"].get_cached_flag(
            "user-123",
            "checkout-redesign",
            current_flag_version=1,
        )
        is None
    )


@given(
    "the SDK is running on a Linux host with distribution metadata",
    target_fixture="linux_host_context",
)
def linux_host_with_distribution_metadata():
    patches = [
        mock.patch.object(utils.sys, "platform", "linux"),
        mock.patch.object(
            utils.platform, "python_implementation", return_value="CPython"
        ),
        mock.patch.object(utils.distro, "info", return_value={"version": "24.04"}),
        mock.patch.object(utils.distro, "name", return_value="Ubuntu"),
    ]
    with ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        yield


@when("the SDK builds system context", target_fixture="system_context")
def build_system_context(linux_host_context):
    return utils.system_context()


@then("the context includes Python runtime and Linux metadata")
def context_includes_python_runtime_and_linux_metadata(system_context):
    assert system_context == {
        "$python_runtime": "CPython",
        "$python_version": f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}",
        "$os": "Linux",
        "$os_version": "24.04",
        "$os_distro": "Ubuntu",
    }
