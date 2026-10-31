"""Reading the ``/flags?v=4`` response, checked against the released wire contract.

The files in ``fixtures/feature_flag_rules_v2`` are copied unchanged from a released
``PostHog/posthog-sdk-test-harness`` contract. ``SOURCE.json`` records the release and
the digest of each file.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest

from posthog import AsyncPosthog
from posthog.client import Client
from posthog.feature_flag_evaluations import FeatureFlagEvaluations
from posthog.test.test_utils import FAKE_TEST_API_KEY
from posthog.types import (
    FeatureFlag,
    FeatureFlagResult,
    FlagMetadata,
    FlagReason,
    _set_private_fields,
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


def test_vendored_fixtures_match_the_recorded_release_digests():
    source = _load("SOURCE.json")
    assert source["release"] == "1.22.0"
    assert source["contract_version"] == "7.0.0"
    for name, entry in source["files"].items():
        digest = hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()
        assert digest == entry["sha256"], name


# The /flags?v=4 metadata fields that stay private until the typed accessors ship.
_PRIVATE_METADATA_FIELDS = {
    "config_version",
    "rule_type",
    "rule_id",
    "variant_key",
    "forced_variant",
}

VALID_RESPONSES = [case for case in RESPONSES["cases"] if case["expected"] == "valid"]


@pytest.mark.parametrize("case", VALID_RESPONSES, ids=lambda case: case["id"])
def test_valid_producer_responses_are_retained_as_sent(case):
    response = _build(RESPONSES, case)
    records = copy.deepcopy(response["flags"])
    flags = normalize_flags_response(response)["flags"]

    assert set(flags) == set(records)
    for key, record in records.items():
        flag = flags[key]
        assert flag._value == record["value"]
        assert flag._failed is record.get("failed", False)
        assert flag.reason is not None
        assert flag.reason.code == record["reason"]["code"]
        assert flag.reason.condition_index == record["reason"]["condition_index"]
        assert isinstance(flag.metadata, FlagMetadata)
        for field, value in record["metadata"].items():
            # Only config version 1 records carry an experiment ID; it is not kept.
            if field not in ("payload", "experiment_id"):
                if field in _PRIVATE_METADATA_FIELDS:
                    field = f"_{field}"
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


def _old_server_response(**records: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "flags": {
            key: {"key": key, "metadata": {"id": 1, "version": 1}, **record}
            for key, record in records.items()
        },
        "errorsWhileComputingFlags": False,
    }


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

    assert snapshot.get_flag("flag") is True
    logged = [r for r in caplog.records if "no typed flag values" in r.getMessage()]
    assert len(logged) == 1


def test_a_failed_record_never_renders_its_value(client):
    response = _build(RESPONSES, {"template": "error"})
    response["flags"]["garden-layout"]["value"] = "compact"
    snapshot = _evaluate(client, response)

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
    assert malformed._failed is True
    assert malformed._value is None
    assert (malformed.enabled, malformed.variant, malformed.get_value()) == (
        False,
        None,
        False,
    )
    # The record keeps that it could not be read, apart from a server failure.
    assert malformed._malformed_field == path
    sibling = flags["sibling"]
    assert sibling._failed is False
    assert sibling._value == "compact"
    assert sibling._malformed_field is None


def test_a_record_that_is_not_an_object_fails_only_that_record():
    response = {"flags": {"broken": ["compact"], "sibling": _record(key="sibling")}}

    flags = normalize_flags_response(response)["flags"]

    assert flags["broken"]._failed is True
    assert flags["broken"].get_value() is False
    assert flags["sibling"]._value == "compact"


def test_unknown_fields_and_null_optional_fields_are_ignored():
    record = _record(**{"future_field": {"nested": True}})
    record["reason"]["future_reason"] = 1
    record["metadata"].update(future_metadata=[1], variant_key=None)
    record["conditions"] = "not read"

    flag = normalize_flags_response({"flags": {"garden-layout": record}})["flags"][
        "garden-layout"
    ]

    assert flag._failed is False
    assert flag._value == "compact"
    assert isinstance(flag.metadata, FlagMetadata)
    assert flag.metadata._variant_key is None


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

    assert flag._failed is False
    assert flag._value == "compact"
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

    assert flag._value == "compact"
    assert isinstance(flag.metadata, FlagMetadata)
    assert flag.metadata._config_version == 1
    assert flag.metadata._rule_type is None
    assert flag.metadata._rule_id is None
    assert flag.metadata._variant_key is None


def test_a_failed_older_server_record_is_kept_as_failed():
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

    assert flag._failed is True
    assert flag._value is False
    assert flag.reason is not None and flag.reason.description == "Timed out"


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

    assert flags["bad"]._failed is True
    assert flags["bad"]._malformed_field == "reason"
    assert flags["good"]._failed is False
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

    assert flags["deep"]._failed is True
    assert flags["deep"]._malformed_field == "value"
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
    assert malformed._failed is True
    assert malformed._malformed_field == path
    assert malformed._value is None
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
    assert (sibling._failed, sibling._value, sibling._malformed_field) == (
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
        metadata=_set_private_fields(
            FlagMetadata(
                id=202,
                payload=None,
                version=9,
                description="Garden layout",
                has_experiment=False,
            ),
            _config_version=1,
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
    assert flags["garden-layout"]._failed is True
    assert flags["garden-layout"]._malformed_field == "key"
    assert flags["sibling"]._failed is False


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
    assert flag._failed is False
    assert flag._value == "compact"


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
        assert flag._failed is True
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


def test_feature_flag_value_defaults_to_the_legacy_value():
    assert FeatureFlag.from_value_and_payload("a", "control", None)._value == "control"
    assert FeatureFlag.from_value_and_payload("a", True, None)._value is True
    assert FeatureFlag.from_value_and_payload("a", False, None)._value is False


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
        new=mock.AsyncMock(
            return_value=_typed_response(
                {"float-flag": 12.5, "object-flag": {"layout": "compact"}}
            )
        ),
    ):
        client = AsyncPosthog("project-key", send=False)
        snapshot = await client.evaluate_flags(DISTINCT_ID)
        await client.shutdown()

    assert snapshot.get_flag("object-flag") is True
    assert snapshot.get_flag_payload("object-flag") == {"layout": "compact"}
    assert snapshot.get_flag("float-flag") is True
    assert snapshot.get_flag_payload("float-flag") == 12.5
