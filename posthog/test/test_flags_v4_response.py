"""Reading the ``/flags?v=4`` response, checked against the released wire contract.

The files in ``fixtures/feature_flag_rules_v2`` are copied unchanged from a released
``PostHog/posthog-sdk-test-harness`` contract. ``SOURCE.json`` records the release and
the digest of each file.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple
from unittest import mock

import pytest

from posthog import AsyncPosthog
from posthog.client import Client
from posthog.feature_flag_evaluations import FeatureFlagEvaluations
from posthog.test.test_utils import FAKE_TEST_API_KEY
from posthog.types import (
    FeatureFlag,
    FeatureFlagResult,
    FlagEvaluationDetails,
    FlagEvaluationErrorCode,
    FlagEvaluationReason,
    FlagMetadata,
    FlagReason,
    UnresolvedFlagReason,
    normalize_flags_response,
    to_flags_and_payloads,
)

FIXTURES = Path(__file__).with_name("fixtures") / "feature_flag_rules_v2"
DISTINCT_ID = "user-1"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


READERS = _load("readers.json")
RESPONSES = _load("responses.json")
LEGACY_PROJECTION = _load("legacy_projection.json")
VALUE_CORPUS = _load("v2_value_evaluation.json")


def _pointer(pointer: str) -> List[str]:
    return [
        token.replace("~1", "/").replace("~0", "~") for token in pointer.split("/")[1:]
    ]


def _build(fixture: Dict[str, Any], case: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a fixture case: copy its template, remove members, then set members."""
    document = copy.deepcopy(fixture["templates"][case["template"]])
    for pointer in case.get("remove", []):
        *parents, last = _pointer(pointer)
        target = document
        for token in parents:
            target = target[token]
        del target[last]
    for pointer, value in case.get("set", {}).items():
        *parents, last = _pointer(pointer)
        target = document
        for token in parents:
            target = target[token]
        target[last] = copy.deepcopy(value)
    return document


@pytest.fixture
def client():
    client = Client(FAKE_TEST_API_KEY, send=False)
    yield client
    client.shutdown()


def _evaluate(client: Client, response: Dict[str, Any]) -> FeatureFlagEvaluations:
    with mock.patch("posthog.client.flags", return_value=copy.deepcopy(response)):
        return client.evaluate_flags(DISTINCT_ID)


def _details(
    snapshot: FeatureFlagEvaluations, key: str, default: Any, expected: Any = None
) -> FlagEvaluationDetails[Any]:
    """Read through the typed accessor that matches the expected value's type."""
    if isinstance(expected, str):
        return snapshot.get_string_details(key, default)
    return snapshot.get_boolean_details(key, default)


def _complete_split(details: FlagEvaluationDetails[Any]) -> bool:
    """Whether the retained details carry a complete experiment split.

    Exposure events are not emitted yet; this checks that the retained details hold
    everything the exposure predicate needs, and nothing from a partial record.
    """
    metadata = details.flag_metadata
    return (
        details.error_code is None
        and metadata.get("config_version") == 2
        and metadata.get("reason_code") == "experiment_split"
        and metadata.get("rule_type") == "experiment"
        and metadata.get("rule_id") is not None
        and metadata.get("variant_key") is not None
    )


def test_vendored_fixtures_match_the_recorded_release_digests():
    source = _load("SOURCE.json")
    assert source["release"] == "1.22.0"
    assert source["contract_version"] == "7.0.0"
    for name, entry in source["files"].items():
        digest = hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()
        assert digest == entry["sha256"], name


@pytest.mark.parametrize("case", READERS["cases"], ids=lambda case: case["id"])
def test_reader_fixture(client, case):
    snapshot = _evaluate(client, _build(READERS, case))
    reader = case["reader"]
    key = reader["requested_key"]

    details = _details(snapshot, key, reader["caller_default"], reader["value"])

    assert details.value == reader["value"]
    assert type(details.value) is type(reader["value"])
    expected_error = reader["error_code"]
    assert details.error_code == (
        FlagEvaluationErrorCode(expected_error) if expected_error else None
    )
    assert details.flag_metadata.get("config_version") == reader["config_version"]
    assert _complete_split(details) is reader["experiment_exposure"]

    for sibling, expected in case.get("sibling_expectations", {}).items():
        sibling_details = snapshot.get_boolean_details(sibling, False)
        assert sibling_details.error_code == FlagEvaluationErrorCode(
            expected["error_code"]
        )
        assert _complete_split(sibling_details) is expected["experiment_exposure"]


VALID_RESPONSES = [case for case in RESPONSES["cases"] if case["expected"] == "valid"]


@pytest.mark.parametrize("case", VALID_RESPONSES, ids=lambda case: case["id"])
def test_valid_producer_responses_are_retained_as_sent(case):
    response = _build(RESPONSES, case)
    records = copy.deepcopy(response["flags"])
    flags = normalize_flags_response(response)["flags"]

    assert set(flags) == set(records)
    for key, record in records.items():
        flag = flags[key]
        assert flag.error_code is not FlagEvaluationErrorCode.PARSE_ERROR
        assert flag.value == record["value"]
        assert flag.failed is record.get("failed", False)
        assert flag.reason is not None
        assert flag.reason.code == record["reason"]["code"]
        assert flag.reason.condition_index == record["reason"]["condition_index"]
        assert isinstance(flag.metadata, FlagMetadata)
        for field, value in record["metadata"].items():
            # Only config version 1 records carry an experiment ID; it is not kept.
            if field not in ("payload", "experiment_id"):
                assert getattr(flag.metadata, field) == value, field


def _typed_response(values: Dict[str, Any]) -> Dict[str, Any]:
    flags = {}
    for index, (key, value) in enumerate(values.items()):
        flags[key] = {
            "key": key,
            "value": value,
            "reason": {
                "code": "targeting_match",
                "condition_index": 0,
                "description": "Matched rule 1",
            },
            "metadata": {
                "id": 300 + index,
                "version": 2,
                "config_version": 2,
                "description": None,
                "payload": None,
                "has_experiment": False,
                "rule_type": "targeted_release",
                "rule_id": f"30000000-0000-4000-8000-00000000000{index}",
            },
        }
    return {
        "flags": flags,
        "errorsWhileComputingFlags": False,
        "requestId": "30000000-0000-4000-8000-000000000000",
        "evaluatedAt": 1800000000000,
    }


TYPED_VALUES = {
    "boolean-flag": True,
    "false-flag": False,
    "null-flag": None,
    "string-flag": "compact",
    "empty-string-flag": "",
    "integer-flag": 7,
    "zero-flag": 0,
    "float-flag": 12.5,
    "object-flag": {"layout": "compact", "columns": [1, 2], "nested": {"on": True}},
    "empty-object-flag": {},
}

ACCESSORS: Dict[str, Callable[[FeatureFlagEvaluations], Callable[..., Any]]] = {
    "boolean": lambda snapshot: snapshot.get_boolean_details,
    "string": lambda snapshot: snapshot.get_string_details,
    "number": lambda snapshot: snapshot.get_number_details,
    "integer": lambda snapshot: snapshot.get_integer_details,
    "float": lambda snapshot: snapshot.get_float_details,
    "object": lambda snapshot: snapshot.get_object_details,
}
DEFAULTS = {
    "boolean": True,
    "string": "default",
    "number": -1,
    "integer": -1,
    "float": -1.0,
    "object": {"d": 1},
}
# The accessors that return each flag's value, and the value they return.
ACCEPTED = {
    "boolean-flag": {"boolean": True},
    "false-flag": {"boolean": False},
    "string-flag": {"string": "compact"},
    "empty-string-flag": {"string": ""},
    "integer-flag": {"number": 7, "integer": 7, "float": 7.0},
    "zero-flag": {"number": 0, "integer": 0, "float": 0.0},
    "float-flag": {"number": 12.5, "float": 12.5},
    "object-flag": {"object": TYPED_VALUES["object-flag"]},
    "empty-object-flag": {"object": {}},
}


@pytest.mark.parametrize("key", list(TYPED_VALUES))
@pytest.mark.parametrize("value_type", list(ACCESSORS))
def test_typed_accessors_never_coerce(client, key, value_type):
    snapshot = _evaluate(client, _typed_response(TYPED_VALUES))
    default = DEFAULTS[value_type]

    details = ACCESSORS[value_type](snapshot)(key, default)

    value = TYPED_VALUES[key]
    assert details.key == key
    assert details.flag_metadata["reason_code"] == "targeting_match"
    assert details.flag_metadata["config_version"] == 2
    accepted = ACCEPTED.get(key, {})
    if value_type in accepted:
        assert details.value == accepted[value_type]
        assert type(details.value) is type(accepted[value_type])
        assert details.error_code is None
        assert details.reason is FlagEvaluationReason.TARGETING_MATCH
    elif value is None or value is False:
        # No value, or false read as another type: the default, without an error.
        assert details.value == default
        assert details.error_code is None
        assert details.reason is FlagEvaluationReason.TARGETING_MATCH
    else:
        assert details.value == default
        assert details.error_code is FlagEvaluationErrorCode.TYPE_MISMATCH
        assert details.reason is FlagEvaluationReason.ERROR


def test_value_accessors_return_the_details_value(client):
    snapshot = _evaluate(client, _typed_response(TYPED_VALUES))

    assert snapshot.get_boolean_value("boolean-flag", False) is True
    assert snapshot.get_boolean_value("false-flag", True) is False
    assert snapshot.get_boolean_value("null-flag", True) is True
    assert snapshot.get_boolean_value("string-flag", True) is True
    assert snapshot.get_string_value("string-flag", "x") == "compact"
    assert snapshot.get_string_value("false-flag", "x") == "x"
    assert snapshot.get_number_value("integer-flag", 0) == 7
    assert snapshot.get_number_value("boolean-flag", 0) == 0
    assert snapshot.get_object_value("object-flag", {}) == TYPED_VALUES["object-flag"]
    assert snapshot.get_object_value("missing-flag", {"a": 1}) == {"a": 1}


def test_typed_details_carry_the_variant_key_and_context(client):
    response = _build(READERS, {"template": "experiment_split"})
    response["flags"]["garden-layout"]["value"] = "compact"
    snapshot = _evaluate(client, response)

    details = snapshot.get_string_details("garden-layout", "default")

    assert details.value == "compact"
    assert details.variant == "arm_a"
    assert details.reason is FlagEvaluationReason.SPLIT
    assert details.flag_metadata["reason_code"] == "experiment_split"
    assert details.flag_metadata["condition_index"] == 0
    assert details.flag_metadata["rule_type"] == "experiment"
    assert details.flag_metadata["rule_id"] == "10000000-0000-4000-8000-000000000001"
    assert details.flag_metadata["variant_key"] == "arm_a"


def test_details_of_an_experiment_split(client):
    response = _build(READERS, {"template": "experiment_split"})
    response["flags"]["garden-layout"]["value"] = True
    snapshot = _evaluate(client, response)

    details = snapshot.get_boolean_details("garden-layout", False)

    assert details == FlagEvaluationDetails(
        key="garden-layout",
        value=True,
        reason=FlagEvaluationReason.SPLIT,
        variant="arm_a",
        flag_metadata={
            "id": 201,
            "version": 7,
            "config_version": 2,
            "reason_code": "experiment_split",
            "condition_index": 0,
            "rule_type": "experiment",
            "rule_id": "10000000-0000-4000-8000-000000000001",
            "variant_key": "arm_a",
        },
    )
    assert str(details.reason) == "SPLIT"


@pytest.mark.parametrize(
    "template, reason",
    [
        ("targeting_match_targeted_release", FlagEvaluationReason.TARGETING_MATCH),
        ("targeting_match_percentage_rollout", FlagEvaluationReason.TARGETING_MATCH),
        ("rollout_miss_percentage_rollout", FlagEvaluationReason.DEFAULT),
        ("rollout_miss_experiment", FlagEvaluationReason.DEFAULT),
        ("experiment_paused", FlagEvaluationReason.DEFAULT),
        ("holdout", FlagEvaluationReason.DEFAULT),
        ("no_rule_match", FlagEvaluationReason.DEFAULT),
        ("missing_group_key", FlagEvaluationReason.ERROR),
        ("dependency_error", FlagEvaluationReason.ERROR),
        ("error", FlagEvaluationReason.ERROR),
        ("v1", FlagEvaluationReason.TARGETING_MATCH),
    ],
)
def test_reason_is_the_openfeature_reason_of_the_reason_code(client, template, reason):
    snapshot = _evaluate(client, _build(READERS, {"template": template}))
    record = _build(READERS, {"template": template})["flags"]["garden-layout"]

    details = snapshot.get_string_details("garden-layout", "default")

    assert details.reason is reason
    assert details.flag_metadata["reason_code"] == record["reason"]["code"]


def test_an_unknown_config_version_2_reason_code_is_unknown(client):
    response = _typed_response({"string-flag": "compact"})
    response["flags"]["string-flag"]["reason"]["code"] = "future_reason"
    snapshot = _evaluate(client, response)

    details = snapshot.get_string_details("string-flag", "default")

    assert (details.value, details.reason) == ("compact", FlagEvaluationReason.UNKNOWN)


def test_details_of_a_null_flag_default(client):
    response = _build(READERS, {"template": "no_rule_match"})
    response["flags"]["garden-layout"]["value"] = None
    snapshot = _evaluate(client, response)

    details = snapshot.get_string_details("garden-layout", "Welcome")

    assert details.value == "Welcome"
    assert details.reason is FlagEvaluationReason.DEFAULT
    assert details.error_code is None
    assert details.variant is None
    assert details.flag_metadata["reason_code"] == "no_rule_match"


def test_a_type_mismatch_keeps_the_metadata_and_drops_the_variant(client):
    snapshot = _evaluate(client, _build(READERS, {"template": "v1"}))

    details = snapshot.get_boolean_details("garden-layout", False)

    assert details.value is False
    assert details.reason is FlagEvaluationReason.ERROR
    assert details.error_code is FlagEvaluationErrorCode.TYPE_MISMATCH
    assert details.error_message == (
        "Flag 'garden-layout' has a string value, not a boolean value."
    )
    assert details.variant is None
    assert details.flag_metadata["config_version"] == 1
    assert details.flag_metadata["reason_code"] == "condition_match"
    assert details.flag_metadata["variant_key"] == "compact"


def test_an_error_drops_the_variant(client):
    response = _build(READERS, {"template": "experiment_split"})
    response["flags"]["garden-layout"]["failed"] = True
    snapshot = _evaluate(client, response)

    details = snapshot.get_boolean_details("garden-layout", True)

    assert details.error_code is FlagEvaluationErrorCode.GENERAL
    assert details.variant is None
    assert details.flag_metadata["variant_key"] == "arm_a"


def test_the_integer_accessor_rejects_a_fractional_number(client):
    snapshot = _evaluate(client, _typed_response({"ratio": 0.25, "count": 2.0}))

    details = snapshot.get_integer_details("ratio", 1)

    assert details.value == 1
    assert details.error_code is FlagEvaluationErrorCode.TYPE_MISMATCH
    assert details.error_message == (
        "Flag 'ratio' has the number 0.25, which is not an integer."
    )
    assert snapshot.get_float_value("ratio", 1.0) == 0.25
    count = snapshot.get_integer_value("count", 0)
    assert (count, type(count)) == (2, int)


@pytest.mark.parametrize(
    "value, expected",
    [
        (3, 3),
        (3.0, 3),
        (-0.0, 0),
        (2**53 - 1, 2**53 - 1),
        (-(2**53 - 1), -(2**53 - 1)),
        (2**53, None),
        (-(2**53), None),
        (2**53 + 1, None),
        (-(2**53 + 1), None),
        (1e21, None),
    ],
)
def test_the_integer_accessor_accepts_only_exact_integers(client, value, expected):
    snapshot = _evaluate(client, _typed_response({"n": value}))

    details = snapshot.get_integer_details("n", 7)

    if expected is None:
        assert details.value == 7
        assert details.error_code is FlagEvaluationErrorCode.TYPE_MISMATCH
        assert "outside the integer range" in details.error_message
        # The number accessor still returns it, as the nearest double.
        assert snapshot.get_number_value("n", 7) == float(value)
    else:
        assert (details.value, type(details.value)) == (expected, int)
        assert details.error_code is None


@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize(
    "magnitude, number",
    [
        (2**53 - 1, 2**53 - 1),
        (2**53, 2.0**53),
        # 2**53 + 1 has no double. The nearest one is 2**53.
        (2**53 + 1, 2.0**53),
    ],
)
def test_the_number_accessor_reads_integers_beyond_the_safe_range_as_doubles(
    client, sign, magnitude, number
):
    value = sign * magnitude
    snapshot = _evaluate(client, _typed_response({"n": value}))

    details = snapshot.get_number_details("n", 7)

    assert details.error_code is None
    assert (details.value, type(details.value)) == (sign * number, type(number))
    assert snapshot.get_number_value("n", 7) == sign * number
    # The float accessor reads every integer as a double.
    assert snapshot.get_float_value("n", 7.0) == float(value)


@pytest.mark.parametrize("sign", [1, -1])
def test_an_integer_beyond_the_range_of_a_double_reads_as_an_infinity(client, sign):
    snapshot = _evaluate(client, _typed_response({"n": sign * 10**400}))

    for method in ("get_number_details", "get_float_details"):
        details = getattr(snapshot, method)("n", 7.0)
        assert details.error_code is None, method
        assert details.value == sign * math.inf, method
    assert snapshot.get_integer_details("n", 7).error_code is (
        FlagEvaluationErrorCode.TYPE_MISMATCH
    )


def test_a_returned_object_cannot_change_the_snapshot(client):
    snapshot = _evaluate(client, _typed_response(TYPED_VALUES))

    snapshot.get_object_value("object-flag", {})["nested"]["on"] = False

    assert snapshot.get_object_value("object-flag", {}) == TYPED_VALUES["object-flag"]
    assert snapshot.get_flag_payload("object-flag") == TYPED_VALUES["object-flag"]


def test_an_object_too_deeply_nested_to_copy_reads_as_the_default(client):
    # Deep enough that copying it exceeds the recursion limit, but shallow enough
    # that every supported Python can decode and encode it as JSON.
    value: Dict[str, Any] = {}
    deep = value
    for _ in range(700):
        deep["nested"] = {}
        deep = deep["nested"]
    response = _typed_response({"deep-flag": value, "object-flag": {"on": True}})
    # Not through _evaluate, which copies the response.
    with mock.patch("posthog.client.flags", return_value=response):
        snapshot = client.evaluate_flags(DISTINCT_ID)

    details = snapshot.get_object_details("deep-flag", {"fallback": True})

    assert details.value == {"fallback": True}
    assert details.error_code is FlagEvaluationErrorCode.PARSE_ERROR
    assert details.reason is FlagEvaluationReason.ERROR
    assert snapshot.get_object_value("deep-flag", {}) == {}
    assert snapshot.get_object_value("object-flag", {}) == {"on": True}


def _old_server_response(**records: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "flags": {
            key: {"key": key, "metadata": {"id": 1, "version": 1}, **record}
            for key, record in records.items()
        },
        "errorsWhileComputingFlags": False,
    }


def test_a_number_flag_on_an_older_server_is_a_type_mismatch_that_names_the_cause(
    client,
):
    response = _old_server_response(
        **{
            "upload-limit": {
                "enabled": True,
                "variant": None,
                "metadata": {"id": 1, "version": 1, "payload": "25"},
            }
        }
    )
    snapshot = _evaluate(client, response)

    details = snapshot.get_number_details("upload-limit", 10)

    assert details.value == 10
    assert details.error_code is FlagEvaluationErrorCode.TYPE_MISMATCH
    assert details.flag_metadata["config_version"] == 1
    assert "server that does not send typed values" in (details.error_message or "")
    assert snapshot.get_flag_payload("upload-limit") == 25


@pytest.mark.parametrize("shape", ["v4", "older"])
def test_one_log_per_client_for_records_without_typed_values(shape, caplog):
    if shape == "v4":
        response = _typed_response({"string-flag": "compact"})
    else:
        response = _old_server_response(
            **{"string-flag": {"enabled": True, "variant": "compact"}}
        )
    client = Client(FAKE_TEST_API_KEY, send=False)
    other_client = Client(FAKE_TEST_API_KEY, send=False)
    try:
        with caplog.at_level("INFO", logger="posthog"):
            for _ in range(2):
                _evaluate(client, response)
            _evaluate(other_client, response)
    finally:
        client.shutdown()
        other_client.shutdown()

    logged = [r for r in caplog.records if "no typed flag values" in r.getMessage()]
    assert len(logged) == (0 if shape == "v4" else 2)


async def test_the_async_client_logs_records_without_typed_values_once(caplog):
    response = _old_server_response(**{"flag": {"enabled": True, "variant": None}})
    with mock.patch(
        "posthog.async_client._async_flags",
        new=mock.AsyncMock(side_effect=lambda *a, **k: copy.deepcopy(response)),
    ):
        client = AsyncPosthog("project-key", send=False)
        with caplog.at_level("INFO", logger="posthog"):
            await client.evaluate_flags(DISTINCT_ID)
            snapshot = await client.evaluate_flags(DISTINCT_ID)
        await client.shutdown()

    assert snapshot.get_boolean_value("flag", False) is True
    logged = [r for r in caplog.records if "no typed flag values" in r.getMessage()]
    assert len(logged) == 1


def _local_client(versions: Optional[Dict[str, int]] = None) -> Client:
    """A client with local definitions; ``versions`` sets a definition's version."""
    client = Client(FAKE_TEST_API_KEY, secret_key="test", send=False)
    groups = [{"properties": [], "rollout_percentage": 100}]
    definitions: List[Dict[str, Any]] = [
        {
            "id": 11,
            "key": "legacy-layout",
            "active": True,
            "filters": {
                "groups": groups,
                "multivariate": {
                    "variants": [{"key": "compact", "rollout_percentage": 100}]
                },
            },
        },
        {"id": 12, "key": "beta", "active": True, "filters": {"groups": groups}},
        {"id": 13, "key": "off", "active": False, "filters": {"groups": groups}},
        {
            "id": 14,
            "key": "no-match",
            "active": True,
            "filters": {"groups": [{"properties": [], "rollout_percentage": 0}]},
        },
    ]
    for definition in definitions:
        if versions and definition["key"] in versions:
            definition["version"] = versions[definition["key"]]
    client.feature_flags = definitions
    return client


@mock.patch("posthog.client.flags")
def test_locally_evaluated_flags_have_a_derived_reason_and_config_version_1(
    patch_flags,
):
    client = _local_client()
    try:
        snapshot = client.evaluate_flags("user-123")
        legacy_layout = snapshot.get_string_details("legacy-layout", "grid")
        beta = snapshot.get_boolean_details("beta", False)
        off = snapshot.get_boolean_details("off", True)
        no_match = snapshot.get_boolean_details("no-match", True)
        mismatch = snapshot.get_number_details("legacy-layout", 1)
    finally:
        client.shutdown()

    patch_flags.assert_not_called()
    assert legacy_layout == FlagEvaluationDetails(
        key="legacy-layout",
        value="compact",
        reason=FlagEvaluationReason.TARGETING_MATCH,
        variant="compact",
        flag_metadata={"id": 11, "config_version": 1, "variant_key": "compact"},
    )
    assert (beta.value, beta.reason) == (True, FlagEvaluationReason.TARGETING_MATCH)
    assert beta.flag_metadata == {"id": 12, "config_version": 1}
    assert (off.value, off.reason) == (False, FlagEvaluationReason.DISABLED)
    assert off.flag_metadata == {
        "id": 13,
        "config_version": 1,
        "reason_code": "flag_disabled",
    }
    assert (no_match.value, no_match.reason) == (False, FlagEvaluationReason.DEFAULT)
    assert mismatch.error_code is FlagEvaluationErrorCode.TYPE_MISMATCH
    assert "config version 1" in (mismatch.error_message or "")


@mock.patch("posthog.client.flags")
def test_locally_evaluated_details_carry_the_definition_version(patch_flags):
    client = _local_client(versions={"legacy-layout": 4, "beta": 6})
    try:
        with mock.patch.object(client, "capture") as capture:
            snapshot = client.evaluate_flags("user-123")
            legacy_layout = snapshot.get_string_details("legacy-layout", "grid")
            beta = snapshot.get_boolean_details("beta", False)
            beta_value = snapshot.get_boolean_value("beta", False)
            off = snapshot.get_boolean_details("off", True)
    finally:
        client.shutdown()

    patch_flags.assert_not_called()
    assert (legacy_layout.value, beta.value, beta_value) == ("compact", True, True)
    assert legacy_layout.flag_metadata == {
        "id": 11,
        "version": 4,
        "config_version": 1,
        "variant_key": "compact",
    }
    assert beta.flag_metadata == {"id": 12, "version": 6, "config_version": 1}
    # A definition without a version leaves it out.
    assert "version" not in off.flag_metadata
    # The flag-called event of the same read reports the same version.
    event_versions = {
        call.kwargs["properties"]["$feature_flag"]: call.kwargs["properties"].get(
            "$feature_flag_version"
        )
        for call in capture.call_args_list
    }
    assert event_versions == {"legacy-layout": 4, "beta": 6, "off": None}


def test_missing_flag_returns_the_default_with_flag_not_found(client):
    snapshot = _evaluate(client, _typed_response({"boolean-flag": True}))

    details = snapshot.get_number_details("missing-flag", 3)

    assert details == FlagEvaluationDetails(
        key="missing-flag",
        value=3,
        reason=FlagEvaluationReason.ERROR,
        error_code=FlagEvaluationErrorCode.FLAG_NOT_FOUND,
        error_message="Flag 'missing-flag' is not in the evaluation.",
    )


def test_an_unresolved_flag_is_not_found_and_names_the_cause():
    snapshot = FeatureFlagEvaluations(
        host=mock.MagicMock(),
        distinct_id="user-123",
        flags={},
        unresolved_flags={"checkout": UnresolvedFlagReason.EXPERIENCE_CONTINUITY},
    )

    details = snapshot.get_string_details("checkout", "x")

    assert details == FlagEvaluationDetails(
        key="checkout",
        value="x",
        reason=FlagEvaluationReason.ERROR,
        error_code=FlagEvaluationErrorCode.FLAG_NOT_FOUND,
        error_message=(
            "Local evaluation could not resolve flag 'checkout' "
            "(experience_continuity)."
        ),
    )


@pytest.mark.parametrize(
    "template, error_code, message",
    [
        ("dependency_error", FlagEvaluationErrorCode.GENERAL, None),
        ("error", FlagEvaluationErrorCode.GENERAL, None),
        ("missing_group_key", FlagEvaluationErrorCode.INVALID_CONTEXT, None),
    ],
)
def test_error_records_return_the_default(client, template, error_code, message):
    snapshot = _evaluate(client, _build(RESPONSES, {"template": template}))

    details = snapshot.get_boolean_details("garden-layout", True)

    assert details.value is True
    assert details.error_code is error_code
    assert details.error_message == message


def test_failed_record_uses_the_reason_description_as_the_error_message(client):
    response = _build(RESPONSES, {"template": "dependency_error"})
    response["flags"]["garden-layout"]["reason"]["description"] = (
        "Dependency 'garden-root' could not be resolved"
    )
    snapshot = _evaluate(client, response)

    details = snapshot.get_string_details("garden-layout", "default")

    assert details.value == "default"
    assert details.error_code is FlagEvaluationErrorCode.GENERAL
    assert details.error_message == "Dependency 'garden-root' could not be resolved"
    # The legacy rendering of a failed record is unchanged: disabled.
    assert snapshot.get_flag("garden-layout") is False


def test_a_failed_record_never_renders_its_value(client):
    response = _build(RESPONSES, {"template": "error"})
    response["flags"]["garden-layout"]["value"] = "compact"
    snapshot = _evaluate(client, response)

    assert snapshot.get_string_value("garden-layout", "default") == "default"
    assert snapshot.get_flag("garden-layout") is False
    assert snapshot.is_enabled("garden-layout", True) is False


def _record(**overrides: Any) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "key": "garden-layout",
        "value": "compact",
        "reason": {"code": "targeting_match", "condition_index": 0},
        "metadata": {
            "id": 201,
            "version": 7,
            "config_version": 2,
            "has_experiment": False,
            "payload": None,
            "rule_type": "targeted_release",
            "rule_id": "10000000-0000-4000-8000-000000000001",
        },
    }
    for path, value in overrides.items():
        *parents, last = path.split(".")
        target = record
        for token in parents:
            target = target[token]
        target[last] = value
    return record


@pytest.mark.parametrize(
    "path, value",
    [
        ("value", []),
        ("value", float("nan")),
        ("failed", "true"),
        ("reason", "targeting_match"),
        ("reason.code", 3),
        ("reason.condition_index", "0"),
        ("reason.condition_index", 0.5),
        ("reason.description", ["Matched"]),
        ("metadata", []),
        ("metadata.id", "201"),
        ("metadata.version", True),
        ("metadata.config_version", "2"),
        ("metadata.description", 1),
        ("metadata.has_experiment", "false"),
        ("metadata.rule_type", 2),
        ("metadata.rule_id", 3),
        ("metadata.experiment_id", "301"),
        ("metadata.variant_key", True),
        ("metadata.forced_variant", "yes"),
    ],
)
def test_a_known_field_with_the_wrong_type_fails_only_that_record(path, value):
    response = {
        "flags": {
            "garden-layout": _record(**{path: value}),
            "sibling": _record(**{"key": "sibling"}),
        }
    }

    flags = normalize_flags_response(response)["flags"]

    malformed = flags["garden-layout"]
    assert malformed.error_code is FlagEvaluationErrorCode.PARSE_ERROR
    assert malformed.failed is True
    assert malformed.value is None
    assert (malformed.enabled, malformed.variant, malformed.get_value()) == (
        False,
        None,
        False,
    )
    assert path.split(".")[0] in (malformed.error_message or "")
    # The record keeps that it could not be read, apart from a server failure.
    assert malformed._malformed_field == path
    sibling = flags["sibling"]
    assert sibling.error_code is None
    assert sibling.value == "compact"
    assert sibling._malformed_field is None


def test_a_record_that_is_not_an_object_fails_only_that_record():
    response = {"flags": {"broken": ["compact"], "sibling": _record(key="sibling")}}

    flags = normalize_flags_response(response)["flags"]

    assert flags["broken"].error_code is FlagEvaluationErrorCode.PARSE_ERROR
    assert flags["sibling"].value == "compact"


def test_unknown_fields_and_null_optional_fields_are_ignored():
    record = _record(**{"future_field": {"nested": True}})
    record["reason"]["future_reason"] = 1
    record["metadata"].update(future_metadata=[1], variant_key=None)
    record["conditions"] = "not read"

    flag = normalize_flags_response({"flags": {"garden-layout": record}})["flags"][
        "garden-layout"
    ]

    assert flag.error_code is None
    assert flag.value == "compact"
    assert isinstance(flag.metadata, FlagMetadata)
    assert flag.metadata.variant_key is None


@pytest.mark.parametrize(
    "field, value",
    [
        ("experiment_id", 301),
        ("holdout_id", 401),
        ("holdout_id", "401"),
    ],
)
def test_experiment_and_holdout_ids_in_the_metadata_are_ignored(field, value):
    # Only config version 1 records carry an experiment ID, and no record carries a
    # holdout ID: experiments link to rules on the server, and holdouts are
    # rule-local. Neither is kept. An experiment ID of the wrong type is a parse
    # error, like any known field; a holdout ID is an unknown key.
    record = _record(**{f"metadata.{field}": value})

    flag = normalize_flags_response({"flags": {"garden-layout": record}})["flags"][
        "garden-layout"
    ]

    assert flag.error_code is None
    assert flag.value == "compact"
    assert isinstance(flag.metadata, FlagMetadata)
    assert not hasattr(flag.metadata, field)


def test_a_record_without_value_is_read_as_an_older_server_record():
    # An older server sends `enabled` and `variant`. Any rule context it carries
    # is discarded, and the config version reads as 1.
    record = {
        "key": "garden-layout",
        "enabled": True,
        "variant": "compact",
        "reason": {"code": "condition_match", "condition_index": 0},
        "metadata": {
            "id": 202,
            "version": 9,
            "payload": None,
            "config_version": 2,
            "rule_type": "experiment",
            "rule_id": "10000000-0000-4000-8000-000000000001",
            "variant_key": "compact",
        },
    }

    flag = normalize_flags_response({"flags": {"garden-layout": record}})["flags"][
        "garden-layout"
    ]

    assert flag.value == "compact"
    assert isinstance(flag.metadata, FlagMetadata)
    assert flag.metadata.config_version == 1
    assert flag.metadata.rule_type is None
    assert flag.metadata.rule_id is None
    assert flag.metadata.variant_key is None


def test_a_failed_older_server_record_returns_the_default():
    record = {
        "key": "garden-layout",
        "enabled": False,
        "variant": None,
        "failed": True,
        "reason": {
            "code": "timeout",
            "condition_index": None,
            "description": "Timed out",
        },
        "metadata": {"id": 202, "version": 9, "payload": None},
    }

    flag = normalize_flags_response({"flags": {"garden-layout": record}})["flags"][
        "garden-layout"
    ]

    assert flag.error_code is FlagEvaluationErrorCode.GENERAL
    assert flag.error_message == "Timed out"


def _older_record(**overrides: Any) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "key": "garden-layout",
        "enabled": True,
        "variant": "compact",
        "failed": False,
        "reason": {
            "code": "condition_match",
            "condition_index": 0,
            "description": "Matched condition set 1",
        },
        "metadata": {
            "id": 202,
            "version": 9,
            "description": "Garden layout",
            "payload": None,
            "has_experiment": False,
        },
    }
    for path, value in overrides.items():
        *parents, last = path.split(".")
        target = record
        for token in parents:
            target = target[token]
        target[last] = value
    return record


def test_one_unreadable_older_server_record_keeps_its_siblings():
    response = {
        "flags": {
            "bad": {
                "enabled": True,
                "variant": None,
                "reason": "oops",
                "metadata": {"id": 1, "version": 1, "payload": None},
            },
            "good": {
                "enabled": True,
                "variant": None,
                "reason": None,
                "metadata": {"id": 2, "version": 1, "payload": None},
            },
        },
        "requestId": "x",
    }

    flags = normalize_flags_response(response)["flags"]

    assert flags["bad"].failed is True
    assert flags["bad"]._malformed_field == "reason"
    assert flags["good"].failed is False
    assert flags["good"].get_value() is True


def test_a_value_too_deeply_nested_to_encode_fails_only_that_record():
    deep: Dict[str, Any] = {}
    inner = deep
    for _ in range(200_000):
        inner["a"] = {}
        inner = inner["a"]
    response = {
        "flags": {
            "deep": _record(key="deep", value=deep),
            "sibling": _record(key="sibling"),
        }
    }

    flags = normalize_flags_response(response)["flags"]

    assert flags["deep"].failed is True
    assert flags["deep"]._malformed_field == "value"
    assert flags["deep"].error_code is FlagEvaluationErrorCode.PARSE_ERROR
    assert flags["deep"].error_message == (
        "The flag record's value is nested too deeply to read."
    )
    assert flags["sibling"].get_value() == "compact"


@pytest.mark.parametrize(
    "path, value",
    [
        ("key", 123),
        ("enabled", "true"),
        ("variant", 3),
        ("failed", "yes"),
        ("reason", "condition_match"),
        ("reason", ""),
        ("reason.code", 3),
        ("reason.condition_index", "0"),
        ("reason.condition_index", 0.5),
        ("reason.description", ["Matched"]),
        ("metadata", []),
        ("metadata.id", "202"),
        ("metadata.version", True),
        ("metadata.description", 1),
        ("metadata.has_experiment", "false"),
    ],
)
def test_a_known_field_with_the_wrong_type_fails_only_that_older_server_record(
    path, value
):
    response = {
        "flags": {
            "garden-layout": _older_record(**{path: value}),
            "sibling": _older_record(key="sibling"),
        }
    }

    flags = normalize_flags_response(response)["flags"]

    malformed = flags["garden-layout"]
    assert malformed.key == "garden-layout"
    assert malformed.failed is True
    assert malformed._malformed_field == path
    assert malformed.value is None
    assert (malformed.enabled, malformed.variant, malformed.get_value()) == (
        False,
        None,
        False,
    )
    sibling = flags["sibling"]
    assert (
        sibling
        == normalize_flags_response(
            {"flags": {"sibling": _older_record(key="sibling")}}
        )["flags"]["sibling"]
    )
    assert (sibling.failed, sibling.value, sibling._malformed_field) == (
        False,
        "compact",
        None,
    )


def test_a_valid_older_server_record_renders_as_before():
    flag = normalize_flags_response({"flags": {"garden-layout": _older_record()}})[
        "flags"
    ]["garden-layout"]

    assert flag == FeatureFlag(
        key="garden-layout",
        enabled=True,
        variant="compact",
        reason=FlagReason(
            code="condition_match",
            condition_index=0,
            description="Matched condition set 1",
        ),
        metadata=FlagMetadata(
            id=202,
            payload=None,
            version=9,
            description="Garden layout",
            has_experiment=False,
            config_version=1,
        ),
    )


@pytest.mark.parametrize("make_record", [_record, _older_record])
def test_a_record_key_of_the_wrong_type_fails_only_that_record(make_record):
    flags = normalize_flags_response(
        {
            "flags": {
                "garden-layout": make_record(key=123),
                "sibling": make_record(key="sibling"),
            }
        }
    )["flags"]

    assert flags["garden-layout"].key == "garden-layout"
    assert flags["garden-layout"].failed is True
    assert flags["garden-layout"]._malformed_field == "key"
    assert flags["sibling"].failed is False


@pytest.mark.parametrize("make_record", [_record, _older_record])
@pytest.mark.parametrize("key", ["other-flag", None])
def test_a_record_is_read_under_its_map_key(make_record, key):
    # Reader rules fail a record only for a field of the wrong JSON type. A string
    # key that differs from the map key is a producer error, so the map key wins.
    record = make_record(key=key)

    flag = normalize_flags_response({"flags": {"garden-layout": record}})["flags"][
        "garden-layout"
    ]

    assert flag.key == "garden-layout"
    assert flag.failed is False
    assert flag.value == "compact"


def test_a_server_failed_record_is_not_marked_unreadable():
    flags = normalize_flags_response(
        {
            "flags": {
                "v4": _build(RESPONSES, {"template": "error"})["flags"][
                    "garden-layout"
                ],
                "older": _older_record(failed=True, enabled=False, variant=None),
            }
        }
    )["flags"]

    for flag in flags.values():
        assert flag.failed is True
        assert flag._malformed_field is None


@pytest.mark.parametrize("make_record", [_record, _older_record])
def test_absent_flag_id_and_version_stay_absent(make_record, client):
    record = make_record()
    record["key"] = "garden-layout"
    del record["metadata"]["id"]
    del record["metadata"]["version"]
    response = {
        "flags": {"garden-layout": record},
        "errorsWhileComputingFlags": False,
        "requestId": "60000000-0000-4000-8000-000000000000",
    }

    flag = normalize_flags_response(copy.deepcopy(response))["flags"]["garden-layout"]
    assert isinstance(flag.metadata, FlagMetadata)
    assert (flag.metadata.id, flag.metadata.version) == (None, None)

    with (
        mock.patch("posthog.client.flags", return_value=copy.deepcopy(response)),
        mock.patch.object(client, "capture") as capture,
    ):
        client.get_feature_flag_result("garden-layout", DISTINCT_ID)
    properties = capture.call_args.kwargs["properties"]
    assert "$feature_flag_id" not in properties
    assert "$feature_flag_version" not in properties

    # A new client, so the event is not deduplicated against the one above.
    snapshot_client = Client(FAKE_TEST_API_KEY, send=False)
    try:
        with mock.patch.object(snapshot_client, "capture") as capture:
            _evaluate(snapshot_client, response).get_flag("garden-layout")
    finally:
        snapshot_client.shutdown()
    properties = capture.call_args.kwargs["properties"]
    assert "$feature_flag_id" not in properties
    assert "$feature_flag_version" not in properties


@pytest.mark.parametrize(
    "value, encoded", [(25, "25"), ({"columns": 2}, '{"columns":2}')]
)
def test_a_number_or_object_value_is_its_own_payload(value, encoded, client):
    # A config version 2 number or object value is its payload; a payload the
    # record also sends is ignored.
    response = _typed_response({"upload-limit": value})
    response["flags"]["upload-limit"]["metadata"]["payload"] = "999"

    flag = normalize_flags_response(copy.deepcopy(response))["flags"]["upload-limit"]
    assert flag.metadata.payload == encoded

    snapshot = _evaluate(client, response)
    assert snapshot.get_flag_payload("upload-limit") == value


def test_a_config_version_1_payload_is_kept():
    response = _typed_response({"bool-flag": True, "string-flag": "compact"})
    for record in response["flags"].values():
        record["metadata"].update(config_version=1, payload='{"columns": 2}')

    flags = normalize_flags_response(response)["flags"]

    assert flags["bool-flag"].metadata.payload == '{"columns": 2}'
    assert flags["string-flag"].metadata.payload == '{"columns": 2}'


def test_typed_details_of_unreadable_and_failed_older_server_records(client):
    response = {
        "flags": {
            "unreadable": _older_record(key="unreadable", reason="oops"),
            "failed": _older_record(
                key="failed",
                enabled=False,
                variant=None,
                failed=True,
                **{"reason.description": "Timed out"},
            ),
            "healthy": _older_record(key="healthy"),
        },
        "errorsWhileComputingFlags": True,
    }
    snapshot = _evaluate(client, response)

    unreadable = snapshot.get_string_details("unreadable", "x")
    assert (unreadable.value, unreadable.error_code) == (
        "x",
        FlagEvaluationErrorCode.PARSE_ERROR,
    )
    assert unreadable.error_message is not None
    assert "reason" in unreadable.error_message
    failed = snapshot.get_string_details("failed", "x")
    assert (failed.value, failed.error_code, failed.error_message) == (
        "x",
        FlagEvaluationErrorCode.GENERAL,
        "Timed out",
    )
    healthy = snapshot.get_string_details("healthy", "x")
    assert (healthy.value, healthy.error_code) == ("compact", None)


@pytest.mark.parametrize("make_record", [_record, _older_record])
def test_typed_details_leave_out_an_absent_flag_id_and_version(make_record, client):
    record = make_record()
    del record["metadata"]["id"]
    del record["metadata"]["version"]
    snapshot = _evaluate(client, {"flags": {"garden-layout": record}})

    details = snapshot.get_string_details("garden-layout", "x")

    assert details.value == "compact"
    assert "id" not in details.flag_metadata
    assert "version" not in details.flag_metadata


def test_a_number_value_wins_over_a_payload_the_record_sends(client):
    response = _typed_response({"upload-limit": 25})
    response["flags"]["upload-limit"]["metadata"]["payload"] = "999"
    snapshot = _evaluate(client, response)

    assert snapshot.get_number_value("upload-limit", 10) == 25
    assert snapshot.get_flag_payload("upload-limit") == 25


def test_feature_flag_value_defaults_to_the_legacy_value():
    assert FeatureFlag.from_value_and_payload("a", "control", None).value == "control"
    assert FeatureFlag.from_value_and_payload("a", True, None).value is True
    assert FeatureFlag.from_value_and_payload("a", False, None).value is False


# Legacy accessor parity: a v4 record and the equivalent older-server record must give
# identical legacy results.


def _legacy_maps(response: Dict[str, Any]) -> Dict[str, Any]:
    maps = to_flags_and_payloads(normalize_flags_response(copy.deepcopy(response)))
    payloads = maps["featureFlagPayloads"] or {}
    return {
        "featureFlags": maps["featureFlags"],
        "featureFlagPayloads": {
            key: json.loads(value) for key, value in payloads.items()
        },
    }


def _v4_record_for_v1_outcome(outcome: Dict[str, Any]) -> Dict[str, Any]:
    """The v4 record of a v1 flag: ``value`` is the variant, or ``enabled`` without one."""
    if outcome["failed"]:
        value = None
    elif outcome["variant"] is not None:
        value = outcome["variant"]
    else:
        value = outcome["enabled"]
    metadata: Dict[str, Any] = {
        "id": outcome["flag_id"],
        "version": outcome["flag_version"],
        "config_version": 1,
        "payload": outcome["payload"],
        "has_experiment": False,
    }
    if outcome["variant"] is not None:
        metadata["variant_key"] = outcome["variant"]
    record = {
        "key": outcome["key"],
        "value": value,
        "reason": outcome["reason"],
        "metadata": metadata,
    }
    if outcome["failed"]:
        record["failed"] = True
    return record


@pytest.mark.parametrize(
    "case", LEGACY_PROJECTION["cases"], ids=lambda case: case["id"]
)
def test_v1_flags_render_the_same_legacy_values_from_both_shapes(case):
    v2_response = case["projections"]["flags_v2"]
    v1_maps = case["projections"]["flags_v1"]
    outcome = case["outcome"]
    v4_flags = (
        {}
        if outcome["omitted"]
        else {outcome["key"]: _v4_record_for_v1_outcome(outcome)}
    )
    v4_response = {
        "errorsWhileComputingFlags": v2_response["errorsWhileComputingFlags"],
        "flags": v4_flags,
    }
    expected = {
        "featureFlags": v1_maps["featureFlags"],
        "featureFlagPayloads": {
            key: json.loads(value)
            for key, value in v1_maps["featureFlagPayloads"].items()
        },
    }

    assert _legacy_maps(v2_response) == expected
    assert _legacy_maps(v4_response) == expected


VALUE_SUCCESS_CASES = [
    case for case in VALUE_CORPUS["cases"] if case["expected"]["status"] == "success"
]


def _value_case_records(case: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    expected, legacy = case["expected"], case["legacy"]
    rule = expected.get("rule")
    reason = {
        "code": expected["reason"],
        "condition_index": rule["index"] if rule else None,
        "description": "Evaluated",
    }
    metadata = {"id": 401, "version": 3, "description": None, "has_experiment": False}
    v4_metadata = {**metadata, "config_version": 2, "payload": None}
    if rule:
        v4_metadata.update(rule_type=rule["rule_type"], rule_id=rule["id"])
    payload = legacy["payload"]
    v2_record = {
        "key": "typed-flag",
        "enabled": legacy["enabled"],
        "variant": legacy["variant"],
        "reason": reason,
        "metadata": {
            **metadata,
            "payload": None if payload is None else json.dumps(payload),
        },
    }
    v4_record = {
        "key": "typed-flag",
        "value": expected["value"],
        "reason": reason,
        "metadata": v4_metadata,
    }
    return v2_record, v4_record


@pytest.mark.parametrize("case", VALUE_SUCCESS_CASES, ids=lambda case: case["id"])
def test_typed_values_render_the_corpus_legacy_cells_from_both_shapes(case):
    v2_record, v4_record = _value_case_records(case)
    legacy = case["legacy"]
    expected = {
        "featureFlags": {"typed-flag": legacy["value"]},
        "featureFlagPayloads": {}
        if legacy["payload"] is None
        else {"typed-flag": legacy["payload"]},
    }

    for record in (v2_record, v4_record):
        maps = _legacy_maps({"flags": {"typed-flag": record}})
        assert maps == expected
        assert type(maps["featureFlags"]["typed-flag"]) is type(legacy["value"])
        flag = normalize_flags_response(
            {"flags": {"typed-flag": copy.deepcopy(record)}}
        )["flags"]["typed-flag"]
        assert (flag.enabled, flag.variant) == (legacy["enabled"], legacy["variant"])
        result = FeatureFlagResult.from_flag_details(flag)
        assert result is not None
        assert result.get_value() == legacy["value"]
        assert result.payload == legacy["payload"]


def test_number_and_object_payloads_are_compact_json_in_wire_order():
    response = _typed_response(
        {"number": 12.5, "object": {"b": "ü", "a": [1, True, None]}}
    )

    payloads = to_flags_and_payloads(normalize_flags_response(response))[
        "featureFlagPayloads"
    ]

    assert payloads == {"number": "12.5", "object": '{"b":"ü","a":[1,true,null]}'}


# Every legacy getter and event, end to end: a v2-shaped and a v4-shaped response for
# the same flags give identical results.

PARITY_FLAGS = [
    # key, v2 record fields, v4 value
    ("bool-on", {"enabled": True, "variant": None}, True),
    ("bool-off", {"enabled": False, "variant": None}, False),
    ("v1-variant", {"enabled": True, "variant": "control"}, "control"),
    ("v2-string", {"enabled": True, "variant": "compact"}, "compact"),
    ("v2-null", {"enabled": False, "variant": None}, None),
    ("v2-number", {"enabled": True, "variant": None}, 12.5),
    ("v2-object", {"enabled": True, "variant": None}, {"layout": "compact"}),
]
PAYLOADS = {"v1-variant": '{"columns": 2}', "v2-number": "12.5"}
PAYLOADS["v2-object"] = '{"layout":"compact"}'


def _parity_response(shape: str) -> Dict[str, Any]:
    flags = {}
    for index, (key, fields, value) in enumerate(PARITY_FLAGS):
        metadata: Dict[str, Any] = {
            "id": 500 + index,
            "version": 4,
            "description": None,
            "has_experiment": key == "v1-variant",
        }
        reason = {
            "code": "condition_match",
            "condition_index": 0,
            "description": "Matched condition set 1",
        }
        if shape == "v2":
            payload = PAYLOADS.get(key)
            flags[key] = {
                "key": key,
                **fields,
                "reason": reason,
                "metadata": {**metadata, "payload": payload},
            }
        else:
            config_version = 1 if key.startswith(("bool", "v1")) else 2
            payload = PAYLOADS.get(key) if config_version == 1 else None
            flags[key] = {
                "key": key,
                "value": value,
                "reason": reason,
                "metadata": {
                    **metadata,
                    "config_version": config_version,
                    "payload": payload,
                },
            }
    return {
        "flags": flags,
        "errorsWhileComputingFlags": False,
        "requestId": "50000000-0000-4000-8000-000000000000",
        "evaluatedAt": 1800000000000,
    }


def _flag_event_properties(message: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in message["properties"].items()
        if key.startswith("$feature")
        or key in ("$active_feature_flags", "locally_evaluated")
    }


def _legacy_outputs(shape: str) -> Dict[str, Any]:
    client = Client(FAKE_TEST_API_KEY, send=False)
    outputs: Dict[str, Any] = {}
    events: List[Any] = []
    keys = [key for key, _, _ in PARITY_FLAGS]

    def response(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return _parity_response(shape)

    def enqueue(message: Dict[str, Any], *args: Any, **kwargs: Any) -> Optional[str]:
        events.append((message["event"], _flag_event_properties(message)))
        return None

    try:
        with (
            mock.patch("posthog.client.flags", side_effect=response),
            mock.patch.object(client, "_enqueue", side_effect=enqueue),
            pytest.warns(DeprecationWarning),
        ):
            outputs["get_feature_variants"] = client.get_feature_variants(DISTINCT_ID)
            outputs["get_feature_payloads"] = client.get_feature_payloads(DISTINCT_ID)
            outputs["get_feature_flags_and_payloads"] = (
                client.get_feature_flags_and_payloads(DISTINCT_ID)
            )
            outputs["get_all_flags"] = client.get_all_flags(DISTINCT_ID)
            outputs["get_all_flags_and_payloads"] = client.get_all_flags_and_payloads(
                DISTINCT_ID
            )
            for key in keys + ["missing"]:
                result = client.get_feature_flag_result(key, DISTINCT_ID)
                outputs[f"get_feature_flag_result:{key}"] = (
                    None
                    if result is None
                    else (result.enabled, result.variant, result.payload, result.reason)
                )
                outputs[f"get_feature_flag:{key}"] = client.get_feature_flag(
                    key, "other-user"
                )
                outputs[f"feature_enabled:{key}"] = client.feature_enabled(
                    key, "third-user"
                )
                outputs[f"get_feature_flag_payload:{key}"] = (
                    client.get_feature_flag_payload(key, DISTINCT_ID)
                )
            snapshot = client.evaluate_flags("snapshot-user")
            for key in keys + ["missing"]:
                outputs[f"snapshot.is_enabled:{key}"] = snapshot.is_enabled(key)
                outputs[f"snapshot.get_flag:{key}"] = snapshot.get_flag(key)
                outputs[f"snapshot.get_flag_payload:{key}"] = snapshot.get_flag_payload(
                    key
                )
            client.capture(
                "snapshot-event", distinct_id="snapshot-user", flags=snapshot
            )
            client.capture(
                "fetched-event", distinct_id="capture-user", send_feature_flags=True
            )
    finally:
        client.shutdown()
    outputs["events"] = events
    return outputs


def test_every_legacy_getter_and_event_is_identical_for_both_shapes():
    v2_outputs = _legacy_outputs("v2")
    v4_outputs = _legacy_outputs("v4")

    assert v4_outputs == v2_outputs
    # Spot-check the legacy rendering itself.
    assert v4_outputs["get_all_flags"] == {
        "bool-on": True,
        "bool-off": False,
        "v1-variant": "control",
        "v2-string": "compact",
        "v2-null": False,
        "v2-number": True,
        "v2-object": True,
    }
    assert v4_outputs["get_feature_flag_payload:v2-object"] == {"layout": "compact"}
    assert v4_outputs["snapshot.get_flag_payload:v2-number"] == 12.5


def test_feature_flag_called_properties_are_unchanged_for_a_v1_flag(client):
    response = _build(READERS, {"template": "v1"})
    with (
        mock.patch("posthog.client.flags", return_value=response),
        mock.patch.object(client, "capture") as capture,
    ):
        result = client.get_feature_flag_result("garden-layout", DISTINCT_ID)

    assert result is not None
    assert (result.enabled, result.variant, result.payload) == (
        True,
        "compact",
        {"columns": 2},
    )
    capture.assert_called_once_with(
        "$feature_flag_called",
        distinct_id=DISTINCT_ID,
        properties={
            "$feature_flag": "garden-layout",
            "$feature_flag_response": "compact",
            "locally_evaluated": False,
            "$feature/garden-layout": "compact",
            "$feature_flag_payload": {"columns": 2},
            "$feature_flag_request_id": "20000000-0000-4000-8000-000000000002",
            "$feature_flag_evaluated_at": 1800000000000,
            "$feature_flag_version": 9,
            "$feature_flag_id": 202,
            "$feature_flag_has_experiment": False,
        },
        groups={},
        disable_geoip=None,
    )


def test_a_type_mismatch_is_an_access_that_sends_no_event(client):
    with mock.patch.object(client, "capture") as capture:
        snapshot = _evaluate(client, _typed_response({"string-flag": "compact"}))
        snapshot.get_number_details("string-flag", 0)

        capture.assert_not_called()
        assert snapshot.only_accessed().keys == ["string-flag"]

        snapshot.get_flag("string-flag")

    capture.assert_called_once()
    properties = capture.call_args.kwargs["properties"]
    assert properties["$feature_flag_response"] == "compact"
    # A type mismatch is not an evaluation error.
    assert "$feature_flag_error" not in properties


# The flag each typed accessor reads in the per-read event option tests, and the
# value it returns.
TYPED_READS: Dict[str, Tuple[str, Any]] = {
    "boolean": ("boolean-flag", True),
    "string": ("string-flag", "compact"),
    "number": ("float-flag", 12.5),
    "integer": ("integer-flag", 7),
    "float": ("float-flag", 12.5),
    "object": ("object-flag", TYPED_VALUES["object-flag"]),
}
TYPED_METHODS = [
    f"get_{value_type}_{form}"
    for value_type in TYPED_READS
    for form in ("value", "details")
]


@contextlib.asynccontextmanager
async def _typed_snapshot(
    client_kind: str,
) -> AsyncIterator[Tuple[FeatureFlagEvaluations, mock.MagicMock]]:
    """Yield a snapshot of ``TYPED_VALUES`` from the sync or async client, and the
    mocked ``capture`` of that client."""
    response = _typed_response(TYPED_VALUES)
    if client_kind == "sync":
        client = Client(FAKE_TEST_API_KEY, send=False)
        try:
            with mock.patch.object(client, "capture") as capture:
                yield _evaluate(client, response), capture
        finally:
            client.shutdown()
        return
    async_client = AsyncPosthog("project-key", send=False)
    try:
        with (
            mock.patch(
                "posthog.async_client._async_flags",
                new=mock.AsyncMock(return_value=response),
            ),
            mock.patch.object(async_client, "capture") as capture,
        ):
            yield await async_client.evaluate_flags(DISTINCT_ID), capture
    finally:
        await async_client.shutdown()


def _typed_read(snapshot: FeatureFlagEvaluations, method: str, **options: Any) -> Any:
    """Read ``method``'s flag from ``TYPED_READS`` and return the value it resolved."""
    value_type = method.split("_")[1]
    key, _ = TYPED_READS[value_type]
    result = getattr(snapshot, method)(key, DEFAULTS[value_type], **options)
    return result.value if method.endswith("_details") else result


def _flag_called_keys(capture: mock.MagicMock) -> List[str]:
    return [
        call.kwargs["properties"]["$feature_flag"]
        for call in capture.call_args_list
        if call.args == ("$feature_flag_called",)
    ]


@pytest.mark.parametrize("client_kind", ["sync", "async"])
@pytest.mark.parametrize("method", TYPED_METHODS)
async def test_a_typed_read_with_events_off_sends_no_event_and_reserves_no_dedupe_key(
    method, client_kind
):
    key, expected = TYPED_READS[method.split("_")[1]]
    async with _typed_snapshot(client_kind) as (snapshot, capture):
        suppressed = _typed_read(snapshot, method, send_feature_flag_events=False)
        assert suppressed == expected
        assert _flag_called_keys(capture) == []
        # A suppressed read is still an access.
        assert snapshot.only_accessed().keys == [key]

        later = _typed_read(snapshot, method)

    assert later == expected
    assert _flag_called_keys(capture) == [key]


@pytest.mark.parametrize("client_kind", ["sync", "async"])
@pytest.mark.parametrize("method", TYPED_METHODS)
async def test_a_typed_read_sends_one_event_by_default(method, client_kind):
    key, expected = TYPED_READS[method.split("_")[1]]
    async with _typed_snapshot(client_kind) as (snapshot, capture):
        first = _typed_read(snapshot, method)
        assert _flag_called_keys(capture) == [key]
        again = _typed_read(snapshot, method, send_feature_flag_events=True)

    assert first == again == expected
    assert _flag_called_keys(capture) == [key]


# A flag of another type for each typed accessor.
MISMATCHED_READS: Dict[str, str] = {
    "boolean": "string-flag",
    "string": "boolean-flag",
    "number": "object-flag",
    "integer": "float-flag",
    "float": "string-flag",
    "object": "integer-flag",
}


def _error_logs(caplog: pytest.LogCaptureFixture) -> List[str]:
    """The SDK's error logs, without the prefix the SDK adds to each."""
    return [
        record.getMessage().removeprefix("[PostHog] ")
        for record in caplog.records
        if record.name == "posthog" and record.levelname == "ERROR"
    ]


@pytest.mark.parametrize("client_kind", ["sync", "async"])
@pytest.mark.parametrize("method", TYPED_METHODS)
async def test_a_typed_read_of_the_wrong_type_sends_no_event_and_logs_once(
    method, client_kind, caplog
):
    value_type = method.split("_")[1]
    key = MISMATCHED_READS[value_type]
    default = DEFAULTS[value_type]
    async with _typed_snapshot(client_kind) as (snapshot, capture):
        with caplog.at_level("ERROR", logger="posthog"):
            results = [getattr(snapshot, method)(key, default) for _ in range(2)]
            details = getattr(snapshot, f"get_{value_type}_details")(key, default)

        assert _flag_called_keys(capture) == []
        # The read still counts as an access.
        assert snapshot.only_accessed().keys == [key]

    values = [r.value if method.endswith("_details") else r for r in results]
    assert values == [default, default]
    assert details.error_code is FlagEvaluationErrorCode.TYPE_MISMATCH
    assert _error_logs(caplog) == [details.error_message]


@pytest.mark.parametrize("client_kind", ["sync", "async"])
@pytest.mark.parametrize(
    "later_read",
    [
        lambda snapshot: snapshot.get_string_value("string-flag", "default"),
        lambda snapshot: snapshot.get_string_details("string-flag", "default"),
        lambda snapshot: snapshot.get_flag("string-flag"),
        lambda snapshot: snapshot.is_enabled("string-flag"),
    ],
    ids=["get_string_value", "get_string_details", "get_flag", "is_enabled"],
)
async def test_a_read_after_a_type_mismatch_reports_the_flag_once(
    later_read, client_kind
):
    async with _typed_snapshot(client_kind) as (snapshot, capture):
        snapshot.get_boolean_value("string-flag", False)
        snapshot.get_number_details("string-flag", 0)
        assert _flag_called_keys(capture) == []

        later_read(snapshot)
        later_read(snapshot)

    assert _flag_called_keys(capture) == ["string-flag"]
    properties = capture.call_args.kwargs["properties"]
    assert properties["$feature_flag_response"] == "compact"
    assert "$feature_flag_error" not in properties


def test_each_wrong_type_of_a_flag_is_logged_once_per_client(caplog):
    response = _typed_response({"string-flag": "compact"})
    client = Client(FAKE_TEST_API_KEY, send=False)
    other_client = Client(FAKE_TEST_API_KEY, send=False)
    try:
        with caplog.at_level("ERROR", logger="posthog"):
            for _ in range(2):
                snapshot = _evaluate(client, response)
                snapshot.get_boolean_value("string-flag", False)
                snapshot.get_number_details("string-flag", 0)
                snapshot.get_boolean_details(
                    "string-flag", False, send_feature_flag_events=False
                )
            _evaluate(other_client, response).get_boolean_value("string-flag", False)
    finally:
        client.shutdown()
        other_client.shutdown()

    assert _error_logs(caplog) == [
        "Flag 'string-flag' has a string value, not a boolean value.",
        "Flag 'string-flag' has a string value, not a number value.",
        "Flag 'string-flag' has a string value, not a boolean value.",
    ]


@pytest.mark.parametrize(
    "key, value_type, flag_error",
    [
        # No value, or false read as another type: the default without an error.
        ("null-flag", "number", None),
        ("false-flag", "string", None),
        ("missing-flag", "boolean", "flag_missing"),
        ("failed-flag", "string", None),
        ("unreadable-flag", "object", None),
    ],
)
def test_a_default_without_a_type_mismatch_still_reports_the_flag(
    client, caplog, key, value_type, flag_error
):
    response = _typed_response(TYPED_VALUES)
    response["flags"]["failed-flag"] = _record(
        key="failed-flag", value=None, failed=True
    )
    response["flags"]["unreadable-flag"] = _record(
        key="unreadable-flag", **{"metadata.id": "202"}
    )
    with mock.patch.object(client, "capture") as capture:
        snapshot = _evaluate(client, response)
        with caplog.at_level("ERROR", logger="posthog"):
            details = ACCESSORS[value_type](snapshot)(key, DEFAULTS[value_type])

    assert details.value == DEFAULTS[value_type]
    assert details.error_code is not FlagEvaluationErrorCode.TYPE_MISMATCH
    assert _flag_called_keys(capture) == [key]
    properties = capture.call_args.kwargs["properties"]
    assert properties.get("$feature_flag_error") == flag_error
    assert _error_logs(caplog) == []


def test_a_throwing_before_send_does_not_break_a_typed_read():
    calls = []

    def before_send(event):
        calls.append(event["event"])
        raise ValueError("boom")

    client = Client(FAKE_TEST_API_KEY, before_send=before_send, send=False)
    snapshot = _evaluate(client, _typed_response({"string-flag": "compact"}))

    assert snapshot.get_string_value("string-flag", "default") == "compact"
    assert calls == ["$feature_flag_called"]


async def test_async_client_keeps_healthy_flags_beside_unreadable_and_sparse_ones():
    sparse = _record(key="sparse")
    del sparse["metadata"]["id"], sparse["metadata"]["version"]
    response = {
        "flags": {
            "bad-older": _older_record(key="bad-older", reason="oops"),
            "bad-v4": _record(key="bad-v4", **{"metadata.id": "202"}),
            "sparse": sparse,
            "healthy": _older_record(key="healthy"),
        },
        "requestId": "x",
    }
    with mock.patch(
        "posthog.async_client._async_flags", new=mock.AsyncMock(return_value=response)
    ):
        client = AsyncPosthog("project-key", send=False)
        with mock.patch.object(client, "capture") as capture:
            snapshot = await client.evaluate_flags(DISTINCT_ID)
            assert snapshot.get_flag("bad-older") is False
            assert snapshot.get_flag("bad-v4") is False
            assert snapshot.get_flag("sparse") == "compact"
            assert snapshot.get_flag("healthy") == "compact"
        await client.shutdown()

    sparse_event = next(
        call.kwargs["properties"]
        for call in capture.call_args_list
        if call.kwargs["properties"]["$feature_flag"] == "sparse"
    )
    assert "$feature_flag_id" not in sparse_event
    assert "$feature_flag_version" not in sparse_event


async def test_async_client_reads_the_v4_response():
    with mock.patch(
        "posthog.async_client._async_flags",
        new=mock.AsyncMock(return_value=_typed_response(TYPED_VALUES)),
    ):
        client = AsyncPosthog("project-key", send=False)
        snapshot = await client.evaluate_flags(DISTINCT_ID)
        await client.shutdown()

    assert snapshot.get_object_value("object-flag", {}) == TYPED_VALUES["object-flag"]
    assert snapshot.get_number_value("float-flag", 0) == 12.5
    assert snapshot.get_flag("float-flag") is True
    assert snapshot.get_flag_payload("float-flag") == 12.5
