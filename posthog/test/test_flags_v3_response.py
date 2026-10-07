"""Reading the ``/flags?v=3`` response, checked against the released wire contract.

The files in ``fixtures/feature_flag_rules_v2`` are copied unchanged from a released
``PostHog/posthog-sdk-test-harness`` contract. ``SOURCE.json`` records the release and
the digest of each file.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
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
    FlagMetadata,
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
    metadata = details.metadata
    return (
        details.error_code is None
        and metadata is not None
        and details.reason is not None
        and metadata.config_version == 2
        and details.reason.code == "experiment_split"
        and metadata.rule_type == "experiment"
        and metadata.rule_id is not None
        and metadata.experiment_id is not None
        and metadata.variant_key is not None
    )


def test_vendored_fixtures_match_the_recorded_release_digests():
    source = _load("SOURCE.json")
    assert source["release"] == "1.13.1"
    assert source["contract_version"] == "2.3.1"
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
    config_version = details.metadata.config_version if details.metadata else None
    assert config_version == reader["config_version"]
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
            if field != "payload":
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
    "object": lambda snapshot: snapshot.get_object_details,
}
DEFAULTS = {"boolean": True, "string": "default", "number": -1, "object": {"d": 1}}
TYPE_OF = {
    "boolean-flag": "boolean",
    "false-flag": "boolean",
    "string-flag": "string",
    "empty-string-flag": "string",
    "integer-flag": "number",
    "zero-flag": "number",
    "float-flag": "number",
    "object-flag": "object",
    "empty-object-flag": "object",
}


@pytest.mark.parametrize("key", list(TYPED_VALUES))
@pytest.mark.parametrize("value_type", list(ACCESSORS))
def test_typed_accessors_never_coerce(client, key, value_type):
    snapshot = _evaluate(client, _typed_response(TYPED_VALUES))
    default = DEFAULTS[value_type]

    details = ACCESSORS[value_type](snapshot)(key, default)

    value = TYPED_VALUES[key]
    assert details.key == key
    assert details.reason is not None and details.reason.code == "targeting_match"
    assert details.metadata is not None and details.metadata.config_version == 2
    if TYPE_OF.get(key) == value_type:
        assert details.value == value
        assert type(details.value) is type(value)
        assert details.error_code is None
    elif value is None or value is False:
        # No value, or false read as another type: the default, without an error.
        assert details.value == default
        assert details.error_code is None
    else:
        assert details.value == default
        assert details.error_code is FlagEvaluationErrorCode.TYPE_MISMATCH


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
    assert details.reason is not None
    assert (details.reason.code, details.reason.condition_index) == (
        "experiment_split",
        0,
    )
    assert details.metadata is not None
    assert details.metadata.rule_type == "experiment"
    assert details.metadata.rule_id == "10000000-0000-4000-8000-000000000001"
    assert details.metadata.experiment_id == 301
    assert details.metadata.variant_key == "arm_a"


def test_missing_flag_returns_the_default_with_flag_not_found(client):
    snapshot = _evaluate(client, _typed_response({"boolean-flag": True}))

    details = snapshot.get_number_details("missing-flag", 3)

    assert details == FlagEvaluationDetails(
        key="missing-flag",
        value=3,
        error_code=FlagEvaluationErrorCode.FLAG_NOT_FOUND,
        error_message="Flag 'missing-flag' is not in the evaluation.",
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
        ("metadata.holdout_id", 4.5),
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
    sibling = flags["sibling"]
    assert sibling.error_code is None
    assert sibling.value == "compact"


def test_a_record_that_is_not_an_object_fails_only_that_record():
    response = {"flags": {"broken": ["compact"], "sibling": _record(key="sibling")}}

    flags = normalize_flags_response(response)["flags"]

    assert flags["broken"].error_code is FlagEvaluationErrorCode.PARSE_ERROR
    assert flags["sibling"].value == "compact"


def test_unknown_fields_and_null_optional_fields_are_ignored():
    record = _record(**{"future_field": {"nested": True}})
    record["reason"]["future_reason"] = 1
    record["metadata"].update(future_metadata=[1], experiment_id=None, holdout_id=None)
    record["conditions"] = "not read"

    flag = normalize_flags_response({"flags": {"garden-layout": record}})["flags"][
        "garden-layout"
    ]

    assert flag.error_code is None
    assert flag.value == "compact"
    assert isinstance(flag.metadata, FlagMetadata)
    assert flag.metadata.experiment_id is None


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
            "experiment_id": 301,
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
    assert flag.metadata.experiment_id is None
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


def test_feature_flag_value_defaults_to_the_legacy_value():
    assert FeatureFlag.from_value_and_payload("a", "control", None).value == "control"
    assert FeatureFlag.from_value_and_payload("a", True, None).value is True
    assert FeatureFlag.from_value_and_payload("a", False, None).value is False


# Legacy accessor parity: a v3 record and the equivalent older-server record must give
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


def _v3_record_for_v1_outcome(outcome: Dict[str, Any]) -> Dict[str, Any]:
    """The v3 record of a v1 flag: ``value`` is the variant, or ``enabled`` without one."""
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
    v3_flags = (
        {}
        if outcome["omitted"]
        else {outcome["key"]: _v3_record_for_v1_outcome(outcome)}
    )
    v3_response = {
        "errorsWhileComputingFlags": v2_response["errorsWhileComputingFlags"],
        "flags": v3_flags,
    }
    expected = {
        "featureFlags": v1_maps["featureFlags"],
        "featureFlagPayloads": {
            key: json.loads(value)
            for key, value in v1_maps["featureFlagPayloads"].items()
        },
    }

    assert _legacy_maps(v2_response) == expected
    assert _legacy_maps(v3_response) == expected


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
    v3_metadata = {**metadata, "config_version": 2, "payload": None}
    if rule:
        v3_metadata.update(rule_type=rule["rule_type"], rule_id=rule["id"])
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
    v3_record = {
        "key": "typed-flag",
        "value": expected["value"],
        "reason": reason,
        "metadata": v3_metadata,
    }
    return v2_record, v3_record


@pytest.mark.parametrize("case", VALUE_SUCCESS_CASES, ids=lambda case: case["id"])
def test_typed_values_render_the_corpus_legacy_cells_from_both_shapes(case):
    v2_record, v3_record = _value_case_records(case)
    legacy = case["legacy"]
    expected = {
        "featureFlags": {"typed-flag": legacy["value"]},
        "featureFlagPayloads": {}
        if legacy["payload"] is None
        else {"typed-flag": legacy["payload"]},
    }

    for record in (v2_record, v3_record):
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


# Every legacy getter and event, end to end: a v2-shaped and a v3-shaped response for
# the same flags give identical results.

PARITY_FLAGS = [
    # key, v2 record fields, v3 value
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
    v3_outputs = _legacy_outputs("v3")

    assert v3_outputs == v2_outputs
    # Spot-check the legacy rendering itself.
    assert v3_outputs["get_all_flags"] == {
        "bool-on": True,
        "bool-off": False,
        "v1-variant": "control",
        "v2-string": "compact",
        "v2-null": False,
        "v2-number": True,
        "v2-object": True,
    }
    assert v3_outputs["get_feature_flag_payload:v2-object"] == {"layout": "compact"}
    assert v3_outputs["snapshot.get_flag_payload:v2-number"] == 12.5


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


def test_typed_accessors_report_access_like_get_flag(client):
    with mock.patch.object(client, "capture") as capture:
        snapshot = _evaluate(client, _typed_response({"string-flag": "compact"}))
        snapshot.get_number_details("string-flag", 0)
        snapshot.get_flag("string-flag")

    capture.assert_called_once()
    properties = capture.call_args.kwargs["properties"]
    assert properties["$feature_flag_response"] == "compact"
    assert snapshot.only_accessed().keys == ["string-flag"]


async def test_async_client_reads_the_v3_response():
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
