"""``$experiment_exposure`` and the attribution of ``$feature_flag_called``.

A remotely evaluated config version 2 record that assigns an experiment variant sends
``$experiment_exposure`` next to its ``$feature_flag_called``, and every config version
2 record adds its rule attribution to ``$feature_flag_called``. The reader fixtures and
event schemas are copied unchanged from a released ``PostHog/posthog-sdk-test-harness``
contract; ``fixtures/feature_flag_rules_v2/SOURCE.json`` records which.
"""

from __future__ import annotations

import copy
import json
import threading
import warnings
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional
from unittest import mock

import jsonschema
import pytest

from posthog import AsyncPosthog
from posthog.client import (
    _MINIMAL_FLAG_CALLED_EVENT_PROPERTIES,
    _MINIMAL_V2_FLAG_CALLED_EVENT_PROPERTIES,
    MAX_DICT_SIZE,
    Client,
)
from posthog.test.test_flags_v4_response import FIXTURES, READERS, _build
from posthog.test.test_utils import FAKE_TEST_API_KEY
from posthog.types import FeatureFlagResult
from posthog.utils import SizeLimitedDict

FLAG = "pricing-page"
DISTINCT_ID = "visitor-42"
RULE = "7f3c2a10-8d4e-4b6a-9c21-5e0f1a2b3c4d"
OTHER_RULE = "2b9d4e61-0c7a-4f35-8e12-6a4b3c2d1e0f"
REQUEST_ID = "5d1e2f30-4a5b-4c6d-8e7f-90a1b2c3d4e5"
OTHER_REQUEST_ID = "6e2f3a41-5b6c-4d7e-9f80-a1b2c3d4e5f6"
EVALUATED_AT = 1_800_000_000_000

EXPOSURE = "$experiment_exposure"
FLAG_CALLED = "$feature_flag_called"
ATTRIBUTION_PROPERTIES = (
    "$feature_flag_config_version",
    "$feature_flag_reason_code",
    "$feature_flag_rule_id",
    "$feature_flag_rule_type",
    "$feature_flag_variant",
)


# $experiment_exposure leaves these out. The config version, rule type and reason code
# stay on the $feature_flag_called of the same read. The schema projection drops
# undeclared properties, so their absence is asserted separately.
EXPOSURE_LEAVES_OUT = (
    "$feature_flag_config_version",
    "$feature_flag_rule_type",
    "$feature_flag_reason_code",
    "$experiment_exposure_source",
)


def _schema(name: str) -> jsonschema.Draft202012Validator:
    schema = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


FLAG_CALLED_SCHEMA = _schema("feature_flag_called_context.schema.json")
EXPOSURE_SCHEMA = _schema("experiment_exposure_properties.schema.json")


def _projection(
    validator: jsonschema.Draft202012Validator, properties: Mapping[str, Any]
) -> Dict[str, Any]:
    """The properties a closed contract projection covers."""
    names = validator.schema["properties"]
    return {key: value for key, value in properties.items() if key in names}


# --- Responses ----------------------------------------------------------------


def _record(
    code: str,
    value: Any = True,
    *,
    key: str = FLAG,
    rule_type: Optional[str] = None,
    rule_id: Optional[str] = None,
    variant_key: Optional[str] = None,
    config_version: int = 2,
    has_experiment: bool = False,
    description: Optional[str] = None,
    failed: bool = False,
) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "id": 77,
        "version": 3,
        "config_version": config_version,
        "description": None,
        "payload": None,
        "has_experiment": has_experiment,
    }
    for field, field_value in (
        ("rule_type", rule_type),
        ("rule_id", rule_id),
        ("variant_key", variant_key),
    ):
        if field_value is not None:
            metadata[field] = field_value
    reason: Dict[str, Any] = {
        "code": code,
        "condition_index": 0 if rule_id else None,
    }
    if description is not None:
        reason["description"] = description
    record = {"key": key, "value": value, "reason": reason, "metadata": metadata}
    if failed:
        record["failed"] = True
    return record


def _split(
    value: Any = "annual",
    variant_key: str = "annual-first",
    rule_id: str = RULE,
    **overrides: Any,
) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "rule_type": "experiment",
        "rule_id": rule_id,
        "variant_key": variant_key,
        "has_experiment": True,
        **overrides,
    }
    return _record("experiment_split", value, **fields)


def _response(
    *records: Dict[str, Any],
    request_id: str = REQUEST_ID,
    evaluated_at: int = EVALUATED_AT,
    **envelope: Any,
) -> Dict[str, Any]:
    return {
        "flags": {record["key"]: record for record in records},
        "errorsWhileComputingFlags": False,
        "requestId": request_id,
        "evaluatedAt": evaluated_at,
        **envelope,
    }


# --- Recording events -----------------------------------------------------------


@dataclass
class _Event:
    name: str
    distinct_id: Any
    properties: Dict[str, Any]
    groups: Any
    property_allowlist: Any
    uuid: Optional[str]


def _record_events(client: Any) -> tuple[Any, List[_Event]]:
    """Patch ``client.capture`` to record each call, then capture as usual."""
    events: List[_Event] = []
    capture = client.capture

    def record(event: str, **kwargs: Any) -> Optional[str]:
        uuid = capture(event, **kwargs)
        events.append(
            _Event(
                name=event,
                distinct_id=kwargs.get("distinct_id"),
                properties=copy.deepcopy(kwargs.get("properties") or {}),
                groups=kwargs.get("groups"),
                property_allowlist=kwargs.get("_property_allowlist"),
                uuid=uuid,
            )
        )
        return uuid

    return mock.patch.object(client, "capture", side_effect=record), events


def _named(events: List[_Event], name: str, key: str = FLAG) -> List[_Event]:
    return [
        event
        for event in events
        if event.name == name and event.properties.get("$feature_flag") == key
    ]


def _attribution(properties: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        name: properties[name] for name in ATTRIBUTION_PROPERTIES if name in properties
    }


# --- Reading through each path ------------------------------------------------------

# Every sync read that sends flag events. Each takes the client, the key, the
# distinct ID and the groups.
SYNC_READS: Dict[str, Callable[..., Any]] = {
    "get_feature_flag": lambda client, key, distinct_id, groups: (
        client.get_feature_flag(key, distinct_id, groups=groups)
    ),
    "feature_enabled": lambda client, key, distinct_id, groups: (
        client.feature_enabled(key, distinct_id, groups=groups)
    ),
    "get_feature_flag_result": lambda client, key, distinct_id, groups: (
        client.get_feature_flag_result(key, distinct_id, groups=groups)
    ),
    "snapshot.get_flag": lambda client, key, distinct_id, groups: (
        client.evaluate_flags(distinct_id, groups=groups).get_flag(key)
    ),
    "snapshot.is_enabled": lambda client, key, distinct_id, groups: (
        client.evaluate_flags(distinct_id, groups=groups).is_enabled(key)
    ),
}
SINGLE_FLAG_READS = ["get_feature_flag", "feature_enabled", "get_feature_flag_result"]
SNAPSHOT_READS = ["snapshot.get_flag", "snapshot.is_enabled"]


@pytest.fixture
def client() -> Iterator[Client]:
    client = Client(FAKE_TEST_API_KEY, send=False)
    yield client
    client.shutdown()


def _serving(*responses: Dict[str, Any]) -> Any:
    """Patch ``/flags`` to answer with each response in turn, the last one after that."""
    remaining = list(responses)

    def answer(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        response = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return copy.deepcopy(response)

    return mock.patch("posthog.client.flags", side_effect=answer)


def _read(
    client: Client,
    path: str,
    key: str = FLAG,
    distinct_id: Any = DISTINCT_ID,
    groups: Optional[Dict[str, Any]] = None,
) -> Any:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return SYNC_READS[path](client, key, distinct_id, groups or {})


def _read_events(
    path: str,
    *responses: Dict[str, Any],
    reads: int = 1,
    client: Optional[Client] = None,
    **read_kwargs: Any,
) -> List[_Event]:
    """Read ``FLAG`` ``reads`` times through ``path``, one response per read."""
    own_client = client is None
    client = client or Client(FAKE_TEST_API_KEY, send=False)
    recording, events = _record_events(client)
    try:
        with _serving(*responses), recording:
            for _ in range(reads):
                _read(client, path, **read_kwargs)
    finally:
        if own_client:
            client.shutdown()
    return events


async def _read_async_events(
    responses: List[Dict[str, Any]],
    reads: List[Callable[[Any], Any]],
) -> List[_Event]:
    """Run the reads on one async client, answering each evaluation with the next
    response, and the last one after that."""
    remaining = list(responses)

    def answer(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        response = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return copy.deepcopy(response)

    with mock.patch(
        "posthog.async_client._async_flags", new=mock.AsyncMock(side_effect=answer)
    ):
        client = AsyncPosthog("project-key", send=False)
        recording, events = _record_events(client)
        with recording:
            for read in reads:
                await read(client)
        await client.shutdown()
    return events


# --- The contract's reader cases --------------------------------------------------------

READER_CASES = [case for case in READERS["cases"] if case["expected"] == "reader"]


def _check_reader_events(case: Dict[str, Any], events: List[_Event]) -> None:
    reader = case["reader"]
    expectations = {reader["requested_key"]: reader}
    expectations.update(case.get("sibling_expectations", {}))
    for key, expected in expectations.items():
        exposures = _named(events, EXPOSURE, key)
        assert len(exposures) == (1 if expected["experiment_exposure"] else 0), key
        [called] = _named(events, FLAG_CALLED, key)
        config_version = expected.get("config_version")
        if expected.get("error_code") == "PARSE_ERROR":
            config_version = None
        assert called.properties.get("$feature_flag_config_version") == (
            2 if config_version == 2 else None
        ), key
        if called.properties["$feature_flag_response"] is not None:
            FLAG_CALLED_SCHEMA.validate(
                _projection(FLAG_CALLED_SCHEMA, called.properties)
            )
        for exposure in exposures:
            EXPOSURE_SCHEMA.validate(_projection(EXPOSURE_SCHEMA, exposure.properties))
            assert not set(EXPOSURE_LEAVES_OUT) & set(exposure.properties), key
            assert (
                exposure.properties["$feature_flag_response"]
                == (called.properties["$feature_flag_response"])
            )
            assert exposure.uuid is not None and exposure.uuid != called.uuid
        for event in [called, *exposures]:
            # A flag may be named anything, so `$feature/<key>` is left out.
            for name in event.properties:
                if not name.startswith("$feature/"):
                    for part in ("experiment_id", "holdout", "seed"):
                        assert part not in name, name


@pytest.mark.parametrize("path", list(SYNC_READS))
@pytest.mark.parametrize("case", READER_CASES, ids=lambda case: case["id"])
def test_reader_cases_send_an_exposure_exactly_when_the_contract_expects(case, path):
    response = _build(READERS, case)
    client = Client(FAKE_TEST_API_KEY, send=False)
    recording, events = _record_events(client)
    try:
        with _serving(response), recording:
            _read(client, path, case["reader"]["requested_key"])
            for sibling in case.get("sibling_expectations", {}):
                _read(client, path, sibling)
    finally:
        client.shutdown()

    _check_reader_events(case, events)


@pytest.mark.parametrize("method", ["get_flag", "is_enabled"])
@pytest.mark.parametrize("case", READER_CASES, ids=lambda case: case["id"])
async def test_async_reader_cases_send_an_exposure_exactly_when_the_contract_expects(
    case, method
):
    keys = [case["reader"]["requested_key"], *case.get("sibling_expectations", {})]

    async def read(client: Any) -> None:
        snapshot = await client.evaluate_flags(DISTINCT_ID)
        for key in keys:
            getattr(snapshot, method)(key)

    events = await _read_async_events([_build(READERS, case)], [read])

    _check_reader_events(case, events)


# --- Attribution on $feature_flag_called --------------------------------------------------

_RULE_ID = "10000000-0000-4000-8000-000000000001"
ATTRIBUTION_CASES = [
    # (reader template or case id, attribution on $feature_flag_called)
    (
        "experiment_split",
        {
            "$feature_flag_config_version": 2,
            "$feature_flag_reason_code": "experiment_split",
            "$feature_flag_rule_id": _RULE_ID,
            "$feature_flag_rule_type": "experiment",
            "$feature_flag_variant": "arm_a",
        },
    ),
    (
        "targeting_match_targeted_release",
        {
            "$feature_flag_config_version": 2,
            "$feature_flag_reason_code": "targeting_match",
            "$feature_flag_rule_id": _RULE_ID,
            "$feature_flag_rule_type": "targeted_release",
        },
    ),
    (
        "targeting_match_percentage_rollout",
        {
            "$feature_flag_config_version": 2,
            "$feature_flag_reason_code": "targeting_match",
            "$feature_flag_rule_id": _RULE_ID,
            "$feature_flag_rule_type": "percentage_rollout",
        },
    ),
    (
        "rollout_miss_percentage_rollout",
        {
            "$feature_flag_config_version": 2,
            "$feature_flag_reason_code": "rollout_miss",
            "$feature_flag_rule_id": _RULE_ID,
            "$feature_flag_rule_type": "percentage_rollout",
        },
    ),
    (
        "rollout_miss_experiment",
        {
            "$feature_flag_config_version": 2,
            "$feature_flag_reason_code": "rollout_miss",
            "$feature_flag_rule_id": _RULE_ID,
            "$feature_flag_rule_type": "experiment",
        },
    ),
    (
        "experiment_paused",
        {
            "$feature_flag_config_version": 2,
            "$feature_flag_reason_code": "experiment_paused",
            "$feature_flag_rule_id": _RULE_ID,
            "$feature_flag_rule_type": "experiment",
        },
    ),
    (
        "holdout",
        {
            "$feature_flag_config_version": 2,
            "$feature_flag_reason_code": "holdout",
            "$feature_flag_rule_id": _RULE_ID,
            "$feature_flag_rule_type": "experiment",
        },
    ),
    # A holdout assigns no variant, even when its record names one.
    (
        "readers.holdout_variant_key",
        {
            "$feature_flag_config_version": 2,
            "$feature_flag_reason_code": "holdout",
            "$feature_flag_rule_id": _RULE_ID,
            "$feature_flag_rule_type": "experiment",
        },
    ),
    (
        "no_rule_match",
        {
            "$feature_flag_config_version": 2,
            "$feature_flag_reason_code": "no_rule_match",
        },
    ),
    (
        "missing_group_key",
        {
            "$feature_flag_config_version": 2,
            "$feature_flag_reason_code": "missing_group_key",
        },
    ),
    (
        "dependency_error",
        {
            "$feature_flag_config_version": 2,
            "$feature_flag_reason_code": "dependency_error",
        },
    ),
    (
        "error",
        {"$feature_flag_config_version": 2, "$feature_flag_reason_code": "error"},
    ),
    # A malformed split keeps its config version and reason, and loses the whole rule
    # tuple.
    *(
        (
            case_id,
            {
                "$feature_flag_config_version": 2,
                "$feature_flag_reason_code": "experiment_split",
            },
        )
        for case_id in (
            "readers.partial_split_missing_rule_type",
            "readers.partial_split_missing_rule_id",
            "readers.partial_split_missing_variant_key",
            "readers.wrong_rule_type_literal",
            "readers.partial_split_string_value",
        )
    ),
    # Config version 1, from either kind of server, is unchanged.
    ("v1", {}),
    ("old_server", {}),
    ("readers.partial_split_missing_config_version", {}),
]


def _attribution_response(source: str) -> Dict[str, Any]:
    if source.startswith("readers."):
        [case] = [case for case in READERS["cases"] if case["id"] == source]
        return _build(READERS, case)
    return _build(READERS, {"template": source})


@pytest.mark.parametrize("path", ["get_feature_flag_result", "snapshot.get_flag"])
@pytest.mark.parametrize(
    "source, expected", ATTRIBUTION_CASES, ids=[c[0] for c in ATTRIBUTION_CASES]
)
def test_flag_called_attribution_follows_the_record(source, expected, path):
    events = _read_events(path, _attribution_response(source), key="garden-layout")

    [called] = _named(events, FLAG_CALLED, "garden-layout")
    assert _attribution(called.properties) == expected
    FLAG_CALLED_SCHEMA.validate(_projection(FLAG_CALLED_SCHEMA, called.properties))
    exposures = _named(events, EXPOSURE, "garden-layout")
    assert len(exposures) == (1 if "$feature_flag_variant" in expected else 0)


# A record whose reason names no rule, or that failed, reports no rule even when it
# carries one. Neither sends an exposure. The last element says whether the record is
# a valid server outcome, which the contract's event schema then accepts.
STRAY_RULE_CASES = [
    ("no_rule_match", {}, True),
    ("missing_group_key", {}, True),
    ("flag_disabled", {}, True),
    ("dependency_error", {"failed": True, "value": None}, True),
    ("error", {"failed": True, "value": None}, True),
    # A failed record reports no rule, whatever its reason code.
    (
        "experiment_split",
        {"failed": True, "value": None, "variant_key": "annual-first"},
        True,
    ),
    ("holdout", {"failed": True, "value": None}, False),
    # A reason code this SDK does not know.
    ("future_reason", {}, False),
]


@pytest.mark.parametrize("path", ["get_feature_flag_result", "snapshot.get_flag"])
@pytest.mark.parametrize(
    "code, fields, valid_outcome",
    STRAY_RULE_CASES,
    ids=[f"{c}-{sorted(f)}" for c, f, _ in STRAY_RULE_CASES],
)
def test_rule_fields_are_sent_only_for_reasons_that_name_a_rule(
    code, fields, valid_outcome, path
):
    fields = {"value": False, **fields}
    record = _record(
        code, rule_type="experiment", rule_id=RULE, has_experiment=True, **fields
    )

    events = _read_events(path, _response(record))

    [called] = _named(events, FLAG_CALLED)
    assert _attribution(called.properties) == {
        "$feature_flag_config_version": 2,
        "$feature_flag_reason_code": code,
    }
    assert _named(events, EXPOSURE) == []
    if valid_outcome:
        FLAG_CALLED_SCHEMA.validate(_projection(FLAG_CALLED_SCHEMA, called.properties))


@pytest.mark.parametrize("method", ["get_flag", "is_enabled"])
async def test_async_a_failed_split_sends_no_exposure(method):
    # The record keeps its complete split metadata, but it failed.
    record = _split(value=None, failed=True)

    async def read(client: Any) -> None:
        snapshot = await client.evaluate_flags(DISTINCT_ID)
        getattr(snapshot, method)(FLAG)

    events = await _read_async_events([_response(record)], [read])

    [called] = _named(events, FLAG_CALLED)
    assert _attribution(called.properties) == {
        "$feature_flag_config_version": 2,
        "$feature_flag_reason_code": "experiment_split",
    }
    assert _named(events, EXPOSURE) == []


@pytest.mark.parametrize("path", ["get_feature_flag_result", "snapshot.get_flag"])
@pytest.mark.parametrize(
    "code, rule_type",
    [
        ("targeting_match", "targeted_release"),
        ("targeting_match", "percentage_rollout"),
        ("rollout_miss", "percentage_rollout"),
        ("rollout_miss", "experiment"),
        ("experiment_paused", "experiment"),
        ("holdout", "experiment"),
    ],
)
def test_rule_matches_misses_pauses_and_holdouts_name_their_rule(code, rule_type, path):
    record = _record(code, False, rule_type=rule_type, rule_id=RULE)

    events = _read_events(path, _response(record))

    [called] = _named(events, FLAG_CALLED)
    assert _attribution(called.properties) == {
        "$feature_flag_config_version": 2,
        "$feature_flag_reason_code": code,
        "$feature_flag_rule_id": RULE,
        "$feature_flag_rule_type": rule_type,
    }
    assert _named(events, EXPOSURE) == []
    FLAG_CALLED_SCHEMA.validate(_projection(FLAG_CALLED_SCHEMA, called.properties))


# The rule ID and rule type are sent together, with a rule type the reason allows, or
# not at all. A call that drops a broken pair keeps its config version and reason code.
BROKEN_RULE_PAIR_CASES = [
    # Half pairs.
    ("targeting_match", {"rule_id": RULE}),
    ("rollout_miss", {"rule_id": RULE}),
    ("experiment_paused", {"rule_id": RULE}),
    ("holdout", {"rule_id": RULE}),
    ("targeting_match", {"rule_type": "targeted_release"}),
    ("rollout_miss", {"rule_type": "percentage_rollout"}),
    ("experiment_paused", {"rule_type": "experiment"}),
    ("holdout", {"rule_type": "experiment"}),
    # Rule types the reason code does not allow.
    ("targeting_match", {"rule_id": RULE, "rule_type": "experiment"}),
    ("rollout_miss", {"rule_id": RULE, "rule_type": "targeted_release"}),
    ("experiment_paused", {"rule_id": RULE, "rule_type": "percentage_rollout"}),
    ("holdout", {"rule_id": RULE, "rule_type": "targeted_release"}),
    # A rule type this SDK does not know.
    ("targeting_match", {"rule_id": RULE, "rule_type": "future_rule_type"}),
]


@pytest.mark.parametrize("path", ["get_feature_flag_result", "snapshot.get_flag"])
@pytest.mark.parametrize(
    "code, fields",
    BROKEN_RULE_PAIR_CASES,
    ids=[f"{c}-{sorted(f.items())}" for c, f in BROKEN_RULE_PAIR_CASES],
)
def test_a_half_or_disallowed_rule_pair_is_not_sent(code, fields, path):
    record = _record(code, False, **fields)

    events = _read_events(path, _response(record))

    [called] = _named(events, FLAG_CALLED)
    assert _attribution(called.properties) == {
        "$feature_flag_config_version": 2,
        "$feature_flag_reason_code": code,
    }
    assert _named(events, EXPOSURE) == []
    FLAG_CALLED_SCHEMA.validate(_projection(FLAG_CALLED_SCHEMA, called.properties))


@pytest.mark.parametrize("path", list(SYNC_READS))
def test_a_targeting_match_that_names_an_experiment_rule_sends_no_exposure(path):
    # Even with a variant key and a linked experiment, a targeting match assigns no
    # arm, so it reports no rule and sends no exposure.
    record = _record(
        "targeting_match",
        "annual",
        rule_type="experiment",
        rule_id=RULE,
        variant_key="annual-first",
        has_experiment=True,
    )

    events = _read_events(path, _response(record))

    [called] = _named(events, FLAG_CALLED)
    assert _attribution(called.properties) == {
        "$feature_flag_config_version": 2,
        "$feature_flag_reason_code": "targeting_match",
    }
    assert _named(events, EXPOSURE) == []
    FLAG_CALLED_SCHEMA.validate(_projection(FLAG_CALLED_SCHEMA, called.properties))


def test_an_older_server_record_reports_no_attribution():
    # A record without a typed value comes from a server before /flags?v=4. Its
    # metadata is not read as config version 2, whatever it says.
    response = _response(
        {
            "key": FLAG,
            "enabled": True,
            "variant": "annual",
            "reason": {"code": "condition_match", "condition_index": 0},
            "metadata": {
                "id": 77,
                "version": 3,
                "config_version": 2,
                "rule_type": "experiment",
                "rule_id": RULE,
                "variant_key": "annual-first",
            },
        }
    )
    for path in SYNC_READS:
        events = _read_events(path, response)
        [called] = _named(events, FLAG_CALLED)
        assert _attribution(called.properties) == {}
        assert _named(events, EXPOSURE) == []


# --- $feature_flag_reason ------------------------------------------------------------------

REASON_DESCRIPTION = "Matched rule 1"


@pytest.mark.parametrize("path", list(SYNC_READS))
@pytest.mark.parametrize(
    "record",
    [
        _record(
            "targeting_match",
            rule_type="targeted_release",
            rule_id=RULE,
            description=REASON_DESCRIPTION,
        ),
        _record("no_rule_match", False, description=REASON_DESCRIPTION),
        _record("error", None, failed=True, description=REASON_DESCRIPTION),
        _split(description=REASON_DESCRIPTION),
    ],
    ids=["targeting_match", "no_rule_match", "failed", "experiment_split"],
)
def test_a_config_version_2_call_reports_its_reason_by_code_only(record, path):
    events = _read_events(path, _response(record))

    [called] = _named(events, FLAG_CALLED)
    assert called.properties["$feature_flag_reason_code"] == record["reason"]["code"]
    assert "$feature_flag_reason" not in called.properties


@pytest.mark.parametrize("path", list(SYNC_READS))
@pytest.mark.parametrize(
    "record",
    [
        _record(
            "condition_match",
            "annual",
            config_version=1,
            description=REASON_DESCRIPTION,
        ),
        # A record from a server before /flags?v=4, which has no config version.
        {
            "key": FLAG,
            "enabled": True,
            "variant": "annual",
            "reason": {
                "code": "condition_match",
                "condition_index": 0,
                "description": REASON_DESCRIPTION,
            },
            "metadata": {"id": 77, "version": 3},
        },
    ],
    ids=["config_version_1", "older_server"],
)
def test_a_call_without_config_version_2_keeps_its_reason_description(record, path):
    events = _read_events(path, _response(record))

    [called] = _named(events, FLAG_CALLED)
    assert called.properties["$feature_flag_reason"] == REASON_DESCRIPTION
    assert _attribution(called.properties) == {}


@pytest.mark.parametrize(
    "config_version, code, reason",
    [(1, "condition_match", REASON_DESCRIPTION), (2, "targeting_match", None)],
)
def test_minimal_calls_keep_the_reason_description_only_without_config_version_2(
    config_version, code, reason
):
    messages: List[Dict[str, Any]] = []

    def before_send(message: Dict[str, Any]) -> Dict[str, Any]:
        messages.append(message)
        return message

    client = Client(FAKE_TEST_API_KEY, send=False, before_send=before_send)
    record = _record(
        code, config_version=config_version, description=REASON_DESCRIPTION
    )
    try:
        with _serving(_response(record, minimalFlagCalledEvents=True)):
            client.evaluate_flags(DISTINCT_ID).get_flag(FLAG)
            client.get_feature_flag_result(FLAG, "visitor-43")
    finally:
        client.shutdown()

    allowlist = (
        _MINIMAL_FLAG_CALLED_EVENT_PROPERTIES
        if config_version == 1
        else _MINIMAL_V2_FLAG_CALLED_EVENT_PROPERTIES
    )
    assert [message["event"] for message in messages] == [FLAG_CALLED, FLAG_CALLED]
    for message in messages:
        assert set(message["properties"]) <= allowlist
        assert message["properties"].get("$feature_flag_reason") == reason


def test_minimal_config_version_1_calls_drop_super_properties_named_like_attribution():
    attribution_names = (
        _MINIMAL_V2_FLAG_CALLED_EVENT_PROPERTIES - _MINIMAL_FLAG_CALLED_EVENT_PROPERTIES
    )
    messages: List[Dict[str, Any]] = []

    def before_send(message: Dict[str, Any]) -> Dict[str, Any]:
        messages.append(message)
        return message

    client = Client(
        FAKE_TEST_API_KEY,
        send=False,
        before_send=before_send,
        super_properties={name: "set-by-the-app" for name in attribution_names},
    )
    record = _record(
        "condition_match", config_version=1, description=REASON_DESCRIPTION
    )
    try:
        with _serving(_response(record, minimalFlagCalledEvents=True)):
            client.get_feature_flag_result(FLAG, DISTINCT_ID)
    finally:
        client.shutdown()

    [message] = messages
    assert not attribution_names & set(message["properties"])


# --- The exact events of a split ---------------------------------------------------------


# The attribution of a split's $feature_flag_called.
EXPECTED_ATTRIBUTION = {
    "$feature_flag_config_version": 2,
    "$feature_flag_reason_code": "experiment_split",
    "$feature_flag_rule_id": RULE,
    "$feature_flag_rule_type": "experiment",
    "$feature_flag_variant": "annual-first",
}
EXPECTED_EXPOSURE = {
    "$feature_flag": FLAG,
    "$feature_flag_response": "annual",
    "$feature_flag_variant": "annual-first",
    "$feature_flag_rule_id": RULE,
    "locally_evaluated": False,
    "$feature_flag_id": 77,
    "$feature_flag_version": 3,
    "$feature_flag_request_id": REQUEST_ID,
    "$feature_flag_evaluated_at": EVALUATED_AT,
}


@pytest.mark.parametrize("path", list(SYNC_READS))
def test_a_split_sends_both_events_with_exact_properties(path):
    response = _response(_split(description="Assigned variant 'annual-first'"))

    events = _read_events(path, response, groups={"company": "company-7"})

    assert [event.name for event in events] == [FLAG_CALLED, EXPOSURE]
    called, exposure = events
    assert called.properties == {
        "$feature_flag": FLAG,
        "$feature_flag_response": "annual",
        "locally_evaluated": False,
        f"$feature/{FLAG}": "annual",
        "$feature_flag_request_id": REQUEST_ID,
        "$feature_flag_evaluated_at": EVALUATED_AT,
        # The reason code replaces the record's reason description.
        "$feature_flag_version": 3,
        "$feature_flag_id": 77,
        "$feature_flag_has_experiment": True,
        **EXPECTED_ATTRIBUTION,
    }
    assert exposure.properties == EXPECTED_EXPOSURE
    assert exposure.distinct_id == called.distinct_id == DISTINCT_ID
    assert exposure.groups == called.groups == {"company": "company-7"}
    assert exposure.property_allowlist is None


async def test_async_split_sends_both_events_with_exact_properties():
    async def read(client: Any) -> None:
        snapshot = await client.evaluate_flags(
            DISTINCT_ID, groups={"company": 7}, disable_geoip=True
        )
        snapshot.get_flag(FLAG)

    response = _response(_split(description="Assigned variant 'annual-first'"))
    events = await _read_async_events([response], [read])

    assert [event.name for event in events] == [FLAG_CALLED, EXPOSURE]
    called, exposure = events
    assert exposure.properties == EXPECTED_EXPOSURE
    assert _attribution(called.properties) == EXPECTED_ATTRIBUTION
    assert "$feature_flag_reason" not in called.properties
    # The async client sends group keys as strings.
    assert exposure.groups == called.groups == {"company": "7"}
    assert exposure.distinct_id == called.distinct_id == DISTINCT_ID


def test_the_pair_shares_capture_identity_but_not_its_uuid():
    messages: List[Dict[str, Any]] = []

    def before_send(message: Dict[str, Any]) -> Dict[str, Any]:
        messages.append(message)
        return message

    client = Client(FAKE_TEST_API_KEY, send=False, before_send=before_send)
    try:
        with _serving(_response(_split())):
            snapshot = client.evaluate_flags(
                DISTINCT_ID, groups={"company": "company-7"}, disable_geoip=True
            )
            assert snapshot.get_flag(FLAG) == "annual"
    finally:
        client.shutdown()

    called, exposure = messages
    assert (called["event"], exposure["event"]) == (FLAG_CALLED, EXPOSURE)
    assert called["uuid"] and exposure["uuid"] and called["uuid"] != exposure["uuid"]
    assert exposure["distinct_id"] == called["distinct_id"] == DISTINCT_ID
    for name in ("$groups", "$lib", "$lib_version", "$geoip_disable"):
        assert exposure["properties"][name] == called["properties"][name], name
    assert exposure["properties"]["$groups"] == {"company": "company-7"}


@pytest.mark.parametrize("path", SNAPSHOT_READS)
def test_an_assignment_is_reported_whatever_the_read_returns(path):
    # An experiment arm whose value is false still assigned the visitor to that arm.
    response = _response(_split(value=False, variant_key="no-banner"))

    events = _read_events(path, response)

    [exposure] = _named(events, EXPOSURE)
    assert exposure.properties["$feature_flag_response"] is False
    assert exposure.properties["$feature_flag_variant"] == "no-banner"


# --- What sends no exposure ------------------------------------------------------------


@pytest.mark.parametrize("path", SINGLE_FLAG_READS)
def test_opting_out_of_flag_events_suppresses_both(client, path):
    recording, events = _record_events(client)
    with _serving(_response(_split())), recording, warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        getattr(client, path)(FLAG, DISTINCT_ID, send_feature_flag_events=False)

    assert events == []


def test_a_payload_read_sends_no_flag_events(client):
    recording, events = _record_events(client)
    with _serving(_response(_split())), recording, warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        client.get_feature_flag_payload(FLAG, DISTINCT_ID)
        snapshot = client.evaluate_flags(DISTINCT_ID)
        snapshot.get_flag_payload(FLAG)

    assert events == []


def test_a_payload_read_that_sends_flag_events_also_sends_the_exposure(client):
    recording, events = _record_events(client)
    with _serving(_response(_split())), recording, warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        client.get_feature_flag_payload(
            FLAG, DISTINCT_ID, send_feature_flag_events=True
        )

    assert [event.name for event in events] == [FLAG_CALLED, EXPOSURE]


MATCH_VALUE_CASES = [
    ("another value", _split(value="annual"), "monthly"),
    ("the record's own value", _split(value="annual"), "annual"),
    # Object and number arms both read as True, so equal values prove nothing about
    # where a value came from.
    ("an object arm", _split(value={"layout": "grid"}), True),
    ("a number arm", _split(value=12.5), True),
]


@pytest.mark.parametrize("later_read", list(SYNC_READS))
@pytest.mark.parametrize(
    "record, match_value",
    [case[1:] for case in MATCH_VALUE_CASES],
    ids=[case[0] for case in MATCH_VALUE_CASES],
)
def test_a_payload_lookup_for_a_match_value_sends_no_exposure(
    client, record, match_value, later_read
):
    # The lookup reports the caller's match_value, not the record's value, so it
    # sends no exposure, whatever the value. A later read of the record does.
    recording, events = _record_events(client)
    with _serving(_response(record)), recording, warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        client.get_feature_flag_payload(
            FLAG, DISTINCT_ID, match_value=match_value, send_feature_flag_events=True
        )
        [called] = events
        assert called.name == FLAG_CALLED
        assert called.properties["$feature_flag_response"] == match_value
        # The call still carries the record's attribution.
        assert _attribution(called.properties) == EXPECTED_ATTRIBUTION

        _read(client, later_read)

    [exposure] = _named(events, EXPOSURE)
    assert exposure.properties["$feature_flag_variant"] == "annual-first"


def test_a_stale_cached_value_sends_no_exposure(client):
    # The record arrived, but caching it failed, so the read reports the stale cached
    # value instead. That value did not come from the record, even though it is equal.
    cache = mock.Mock()
    cache._set_cached_remote_flag.side_effect = RuntimeError("cache unavailable")
    cache.get_stale_cached_flag.return_value = FeatureFlagResult(
        key=FLAG, enabled=True, variant="annual", payload=None, reason=None
    )
    client.flag_cache = cache
    recording, events = _record_events(client)
    with _serving(_response(_split())), recording:
        result = client.get_feature_flag_result(FLAG, DISTINCT_ID)

    assert result is not None and result.get_value() == "annual"
    cache.get_stale_cached_flag.assert_called_once()
    assert [event.name for event in events] == [FLAG_CALLED]


@pytest.mark.parametrize("path", list(SYNC_READS))
def test_a_locally_evaluated_flag_is_unchanged(path):
    client = Client(FAKE_TEST_API_KEY, send=False, secret_key="secret-key")
    client.feature_flags = [
        {
            "id": 9,
            "name": "Pricing page",
            "key": FLAG,
            "active": True,
            "filters": {
                "groups": [{"properties": [], "rollout_percentage": 100}],
                "multivariate": {
                    "variants": [{"key": "annual", "rollout_percentage": 100}]
                },
            },
        }
    ]
    recording, events = _record_events(client)
    try:
        with _serving(_response(_split())) as remote, recording:
            _read(client, path)
    finally:
        client.shutdown()

    assert remote.call_count == 0
    [called] = events
    assert called.name == FLAG_CALLED
    assert called.properties["locally_evaluated"] is True
    assert _attribution(called.properties) == {}


def test_an_empty_snapshot_sends_nothing(client):
    recording, events = _record_events(client)
    with _serving(_response(_split())), recording:
        snapshot = client.evaluate_flags()
        snapshot.get_flag(FLAG)
        snapshot.is_enabled(FLAG)

    assert events == []


def test_a_failed_exposure_capture_does_not_change_the_read():
    def before_send(message: Dict[str, Any]) -> Dict[str, Any]:
        if message["event"] == EXPOSURE:
            raise ValueError("rejected")
        return message

    client = Client(FAKE_TEST_API_KEY, send=False, before_send=before_send)
    try:
        with _serving(_response(_split())), warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            assert client.get_feature_flag(FLAG, DISTINCT_ID) == "annual"
            assert client.evaluate_flags(DISTINCT_ID).get_flag(FLAG) == "annual"
    finally:
        client.shutdown()


# --- Dedupe ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "first, second",
    [
        ("get_feature_flag", "get_feature_flag"),
        ("snapshot.get_flag", "snapshot.is_enabled"),
        ("feature_enabled", "snapshot.get_flag"),
        ("snapshot.get_flag", "get_feature_flag_result"),
    ],
)
def test_a_repeated_read_sends_nothing_new(client, first, second):
    # The second evaluation has a new request ID and time, which neither event's
    # dedupe reads.
    later = _response(
        _split(), request_id=OTHER_REQUEST_ID, evaluated_at=EVALUATED_AT + 1
    )
    recording, events = _record_events(client)
    with _serving(_response(_split()), later), recording:
        _read(client, first)
        _read(client, second)

    assert [event.name for event in events] == [FLAG_CALLED, EXPOSURE]


@pytest.mark.parametrize("path", list(SYNC_READS))
def test_equal_valued_arms_are_each_reported(path):
    # An A/A experiment: both arms return the same value.
    events = _read_events(
        path,
        _response(_split(value=True, variant_key="control")),
        _response(_split(value=True, variant_key="test")),
        reads=2,
    )

    exposures = _named(events, EXPOSURE)
    assert [e.properties["$feature_flag_variant"] for e in exposures] == [
        "control",
        "test",
    ]
    assert len(_named(events, FLAG_CALLED)) == 2


@pytest.mark.parametrize("path", list(SYNC_READS))
def test_a_later_experiment_on_the_flag_is_reported_again(path):
    events = _read_events(
        path,
        _response(_split(rule_id=RULE)),
        _response(_split(rule_id=OTHER_RULE)),
        reads=2,
    )

    exposures = _named(events, EXPOSURE)
    assert [e.properties["$feature_flag_rule_id"] for e in exposures] == [
        RULE,
        OTHER_RULE,
    ]
    assert len(_named(events, FLAG_CALLED)) == 2


@pytest.mark.parametrize("path", list(SYNC_READS))
def test_the_exposure_dedupe_leaves_out_the_value(path):
    # The arm's value changed, but the visitor is still in the same arm.
    events = _read_events(
        path,
        _response(_split(value="annual")),
        _response(_split(value="annual-v2")),
        reads=2,
    )

    assert len(_named(events, FLAG_CALLED)) == 2
    assert len(_named(events, EXPOSURE)) == 1


@pytest.mark.parametrize("path", list(SYNC_READS))
def test_a_holdout_and_a_split_of_one_rule_are_both_reported(path):
    holdout = _record(
        "holdout", False, rule_type="experiment", rule_id=RULE, has_experiment=True
    )
    events = _read_events(
        path, _response(holdout), _response(_split(value=False)), reads=2
    )

    called = _named(events, FLAG_CALLED)
    assert [e.properties["$feature_flag_reason_code"] for e in called] == [
        "holdout",
        "experiment_split",
    ]
    assert len(_named(events, EXPOSURE)) == 1


def test_groups_are_part_of_the_exposure_identity(client):
    recording, events = _record_events(client)
    with _serving(_response(_split())), recording:
        _read(client, "snapshot.get_flag", groups={"company": "a", "team": "b"})
        _read(client, "get_feature_flag", groups={"team": "b", "company": "a"})
        _read(client, "snapshot.get_flag", groups={"company": "c"})

    exposures = _named(events, EXPOSURE)
    assert [e.groups for e in exposures] == [
        {"company": "a", "team": "b"},
        {"company": "c"},
    ]


def test_the_exposure_dedupe_normalizes_the_distinct_id(client):
    recording, events = _record_events(client)
    with _serving(_response(_split())), recording:
        _read(client, "get_feature_flag_result", distinct_id=42)
        _read(client, "snapshot.get_flag", distinct_id="42")

    assert len(_named(events, EXPOSURE)) == 1


def test_a_reported_call_never_suppresses_its_exposure(client):
    # A read whose value the record did not supply reports the call without an
    # exposure. A later read of the same record still sends the exposure, although
    # its call is a duplicate.
    with _serving(_response(_split())):
        details = client.evaluate_flags(DISTINCT_ID)._flags[FLAG].details
    recording, events = _record_events(client)
    with recording:
        for value_from_details in (False, True):
            client._capture_feature_flag_called_if_needed(
                distinct_id=DISTINCT_ID,
                key=FLAG,
                response="annual",
                properties={"$feature_flag": FLAG, "$feature_flag_response": "annual"},
                details=details,
                value_from_details=value_from_details,
            )

    assert [event.name for event in events] == [FLAG_CALLED, EXPOSURE]


async def test_async_a_reported_call_never_suppresses_its_exposure():
    async def read(client: Any) -> None:
        snapshot = await client.evaluate_flags(DISTINCT_ID)
        details = snapshot._flags[FLAG].details
        for value_from_details in (False, True):
            client._capture_feature_flag_called_if_needed(
                distinct_id=DISTINCT_ID,
                key=FLAG,
                response="annual",
                properties={"$feature_flag": FLAG, "$feature_flag_response": "annual"},
                details=details,
                value_from_details=value_from_details,
            )

    events = await _read_async_events([_response(_split())], [read])

    assert [event.name for event in events] == [FLAG_CALLED, EXPOSURE]


async def test_async_snapshots_dedupe_both_events():
    async def read(client: Any) -> None:
        snapshot = await client.evaluate_flags(DISTINCT_ID)
        snapshot.get_flag(FLAG)
        snapshot.is_enabled(FLAG)
        snapshot.only([FLAG]).get_flag(FLAG)

    events = await _read_async_events(
        [
            _response(_split(variant_key="control", value=True)),
            _response(_split(variant_key="control", value=True)),
            _response(_split(variant_key="test", value=True)),
        ],
        [read, read, read],
    )

    assert [e.properties["$feature_flag_variant"] for e in events] == [
        "control",
        "control",
        "test",
        "test",
    ]
    assert [e.name for e in events] == [FLAG_CALLED, EXPOSURE] * 2


def test_the_v1_dedupe_key_is_unchanged(client):
    with _serving(_build(READERS, {"template": "v1"})):
        _read(client, "get_feature_flag", "garden-layout")

    assert client.distinct_ids_feature_flags_reported[DISTINCT_ID] == {
        ("garden-layout", "compact", ())
    }
    assert len(client._experiment_exposures_reported) == 0


def test_shutdown_clears_the_exposure_dedupe():
    client = Client(FAKE_TEST_API_KEY, send=False)
    with _serving(_response(_split())):
        _read(client, "snapshot.get_flag")
    assert len(client._experiment_exposures_reported) == 1

    client.shutdown()

    assert len(client._experiment_exposures_reported) == 0


async def test_async_shutdown_clears_the_exposure_dedupe():
    with mock.patch(
        "posthog.async_client._async_flags",
        new=mock.AsyncMock(return_value=_response(_split())),
    ):
        client = AsyncPosthog("project-key", send=False)
        (await client.evaluate_flags(DISTINCT_ID)).get_flag(FLAG)
        assert len(client._experiment_exposures_reported) == 1
        await client.shutdown()

    assert len(client._experiment_exposures_reported) == 0


# --- Minimal events -------------------------------------------------------------------


def test_minimal_flag_called_events_keep_the_attribution():
    messages: List[Dict[str, Any]] = []

    def before_send(message: Dict[str, Any]) -> Dict[str, Any]:
        messages.append(message)
        return message

    client = Client(
        FAKE_TEST_API_KEY,
        send=False,
        before_send=before_send,
        super_properties={"plan": "team"},
    )
    # A split of an experiment rule no experiment links to: the flag reports no
    # experiment, so its $feature_flag_called is minimal.
    response = _response(_split(has_experiment=False), minimalFlagCalledEvents=True)
    try:
        with _serving(response):
            client.evaluate_flags(DISTINCT_ID).get_flag(FLAG)
    finally:
        client.shutdown()

    called, exposure = messages
    assert set(called["properties"]) <= _MINIMAL_V2_FLAG_CALLED_EVENT_PROPERTIES
    assert "plan" not in called["properties"]
    assert _attribution(called["properties"]) == EXPECTED_ATTRIBUTION
    # The exposure is a full event.
    assert exposure["event"] == EXPOSURE
    assert exposure["properties"]["plan"] == "team"
    assert _attribution(exposure["properties"]) == _attribution(EXPECTED_EXPOSURE)


# --- Reason codes, rule IDs and variant keys ---------------------------------------------


@pytest.mark.parametrize("path", ["get_feature_flag_result", "snapshot.get_flag"])
@pytest.mark.parametrize("code", ["future_reason", None], ids=["unknown", "missing"])
def test_an_unknown_or_missing_reason_code_still_reports_config_version_2(code, path):
    record = _split()
    if code is None:
        del record["reason"]
    else:
        record["reason"]["code"] = code

    events = _read_events(path, _response(record))

    [called] = _named(events, FLAG_CALLED)
    expected: Dict[str, Any] = {"$feature_flag_config_version": 2}
    if code is not None:
        expected["$feature_flag_reason_code"] = code
    assert _attribution(called.properties) == expected
    assert "$feature_flag_reason" not in called.properties
    assert _named(events, EXPOSURE) == []
    FLAG_CALLED_SCHEMA.validate(_projection(FLAG_CALLED_SCHEMA, called.properties))


NOT_A_UUID = ["rule-1", "", "7F3C2A108D4E4B6A9C215E0F1A2B3C4D", "{" + RULE + "}"]


@pytest.mark.parametrize("path", ["get_feature_flag_result", "snapshot.get_flag"])
@pytest.mark.parametrize("rule_id", NOT_A_UUID)
def test_a_rule_id_that_is_not_a_uuid_counts_as_missing(rule_id, path):
    for record in (
        _split(rule_id=rule_id),
        _record("targeting_match", rule_type="targeted_release", rule_id=rule_id),
    ):
        events = _read_events(path, _response(record))

        [called] = _named(events, FLAG_CALLED)
        assert _attribution(called.properties) == {
            "$feature_flag_config_version": 2,
            "$feature_flag_reason_code": record["reason"]["code"],
        }
        assert _named(events, EXPOSURE) == []
        FLAG_CALLED_SCHEMA.validate(_projection(FLAG_CALLED_SCHEMA, called.properties))


def test_an_upper_case_uuid_rule_id_is_kept():
    events = _read_events(
        "get_feature_flag_result", _response(_split(rule_id=RULE.upper()))
    )

    [exposure] = _named(events, EXPOSURE)
    assert exposure.properties["$feature_flag_rule_id"] == RULE.upper()


@pytest.mark.parametrize("path", list(SYNC_READS))
def test_an_empty_variant_key_is_a_complete_split(path):
    events = _read_events(path, _response(_split(variant_key="")))

    [called] = _named(events, FLAG_CALLED)
    assert called.properties["$feature_flag_variant"] == ""
    [exposure] = _named(events, EXPOSURE)
    assert exposure.properties["$feature_flag_variant"] == ""
    EXPOSURE_SCHEMA.validate(_projection(EXPOSURE_SCHEMA, exposure.properties))


# --- Capture failures ----------------------------------------------------------------------


def _dropping(event: str) -> Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """A before_send hook that drops the first ``event`` and keeps everything else."""
    dropped: List[str] = []

    def before_send(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if message["event"] == event and not dropped:
            dropped.append(event)
            return None
        return message

    return before_send


def _failing_once(client: Client, event: str, failure: str) -> Any:
    """Patch ``client.capture`` to fail for the first ``event``, then capture as usual.

    ``failure`` is ``"raise"`` for a raising capture, or ``"unqueued"`` for a capture
    that returns ``None`` without ``before_send`` dropping the event, as a full or
    closed queue, opt-out or a disabled client does.
    """
    capture = client.capture
    failed: List[str] = []

    def maybe_fail(name: str, **kwargs: Any) -> Optional[str]:
        if name == event and not failed:
            failed.append(name)
            if failure == "raise":
                raise RuntimeError("capture failed")
            return None
        return capture(name, **kwargs)

    return mock.patch.object(client, "capture", side_effect=maybe_fail)


def _sent(client: Client, path: str, reads: int = 2) -> List[str]:
    """The events that reached the queue over ``reads`` reads of a complete split."""
    sent: List[str] = []
    enqueue = client._enqueue

    def record(msg: Dict[str, Any], *args: Any, **kwargs: Any) -> Optional[str]:
        uuid = enqueue(msg, *args, **kwargs)
        if uuid is not None:
            sent.append(msg["event"])
        return uuid

    with (
        _serving(_response(_split())),
        mock.patch.object(client, "_enqueue", side_effect=record),
    ):
        for _ in range(reads):
            assert _read(client, path) == "annual"
    return sent


@pytest.mark.parametrize("path", ["get_feature_flag", "snapshot.get_flag"])
@pytest.mark.parametrize("event", [FLAG_CALLED, EXPOSURE])
def test_an_event_dropped_by_before_send_keeps_its_dedupe_key(event, path):
    client = Client(FAKE_TEST_API_KEY, send=False, before_send=_dropping(event))
    try:
        sent = _sent(client, path)
    finally:
        client.shutdown()

    # The application dropped the event, so the second read does not send it again.
    # The other event of the first read is still sent, once.
    assert sent == [EXPOSURE if event == FLAG_CALLED else FLAG_CALLED]


async def test_async_an_event_dropped_by_before_send_keeps_its_dedupe_key():
    seen: List[str] = []
    batches: List[Any] = []

    def before_send(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        seen.append(message["event"])
        return None if message["event"] == EXPOSURE else message

    async def batch_post(*args: Any, **kwargs: Any) -> None:
        batches.append(kwargs["batch"])

    with (
        mock.patch(
            "posthog.async_client._async_flags",
            new=mock.AsyncMock(return_value=_response(_split())),
        ),
        mock.patch("posthog._async_consumer.async_batch_post", side_effect=batch_post),
    ):
        client = AsyncPosthog("project-key", before_send=before_send, flush_interval=30)
        for _ in range(2):
            snapshot = await client.evaluate_flags(DISTINCT_ID)
            assert snapshot.get_flag(FLAG) == "annual"
            await client.flush(timeout_seconds=1)
        await client.shutdown()

    # before_send saw each event once: the dropped exposure kept its key, so the
    # second read sent neither event again.
    assert seen == [FLAG_CALLED, EXPOSURE]
    sent = [event["event"] for batch in batches for event in batch]
    assert sent == [FLAG_CALLED]


@pytest.mark.parametrize("path", ["get_feature_flag", "snapshot.get_flag"])
@pytest.mark.parametrize("event", [FLAG_CALLED, EXPOSURE])
@pytest.mark.parametrize("failure", ["raise", "unqueued"])
def test_a_failed_capture_keeps_no_dedupe_key(failure, event, path, client):
    with _failing_once(client, event, failure):
        sent = _sent(client, path)

    # The other event of the first read is still sent, and the failed one is sent
    # by the second read, once.
    assert sorted(sent) == sorted([FLAG_CALLED, EXPOSURE])


def test_an_unqueued_config_version_1_call_is_deduped_as_before(client):
    record = _record("condition_match", "annual", config_version=1)
    calls: List[str] = []

    def unqueued(name: str, **kwargs: Any) -> None:
        calls.append(name)
        return None

    with (
        _serving(_response(record)),
        mock.patch.object(client, "capture", side_effect=unqueued),
    ):
        _read(client, "get_feature_flag")
        _read(client, "get_feature_flag")

    # Config version 1 dedupe is unchanged: the attempt takes the key.
    assert calls == [FLAG_CALLED]


class _Rendezvous:
    """Holds each of the first two callers until the other one arrives.

    When the dedupe check and the reservation are one step, the second read waits
    for the first one to finish both, so the wait times out instead.
    """

    def __init__(self) -> None:
        self._barrier = threading.Barrier(2, timeout=0.25)
        self._lock = threading.Lock()
        self.arrivals = 0

    def __call__(self) -> None:
        with self._lock:
            self.arrivals += 1
            arrival = self.arrivals
        if arrival <= 2:
            try:
                self._barrier.wait()
            except threading.BrokenBarrierError:
                pass


class _MeetingSet(set):
    """A dedupe set: each membership check reads the set, then meets the other read."""

    def __init__(self, meet: _Rendezvous) -> None:
        super().__init__()
        self._meet = meet

    def __contains__(self, item: Any) -> bool:
        found = super().__contains__(item)
        self._meet()
        return found


class _MeetingDict(SizeLimitedDict):
    """A dedupe map: each lookup reads the map, then meets the other read."""

    def __init__(self, meet: _Rendezvous) -> None:
        super().__init__(MAX_DICT_SIZE, set)
        self._meet = meet

    def get(self, key: Any, default: Any = None) -> Any:
        found = super().get(key, default)
        self._meet()
        return found


@pytest.mark.parametrize("event", [FLAG_CALLED, EXPOSURE])
@pytest.mark.parametrize("distinct_id", ["seen", "new"])
def test_concurrent_reads_of_one_outcome_send_each_event_once(
    client, event, distinct_id
):
    # Two reads of one outcome both look up the dedupe key before either goes on.
    # Unless the check and the reservation are one step, both find no key and both
    # send the event. "new" also races on the distinct ID's first entry.
    meet = _Rendezvous()
    attribute = (
        "distinct_ids_feature_flags_reported"
        if event == FLAG_CALLED
        else "_experiment_exposures_reported"
    )
    if distinct_id == "seen":
        getattr(client, attribute)[DISTINCT_ID] = _MeetingSet(meet)
    else:
        setattr(client, attribute, _MeetingDict(meet))
    recording, events = _record_events(client)
    with _serving(_response(_split())), recording:
        reads = [
            threading.Thread(target=_read, args=(client, "get_feature_flag"))
            for _ in range(2)
        ]
        for read in reads:
            read.start()
        for read in reads:
            read.join(5)

    assert meet.arrivals == 2
    sent = [e.name for e in events if e.uuid is not None]
    assert sorted(sent) == sorted([FLAG_CALLED, EXPOSURE])


@pytest.mark.parametrize("delivered", [True, False], ids=["delivered", "failed"])
def test_a_read_during_a_sync_mode_send_does_not_send_it_again(delivered):
    # The second read runs while the first read's exposure is on the wire. It sends
    # neither event. A send that fails releases the key, so a third read sends the
    # exposure.
    client = Client(FAKE_TEST_API_KEY, sync_mode=True, capture_mode="v0")
    sending = threading.Event()
    finish = threading.Event()
    posted: List[str] = []

    def batch_post(*args: Any, batch: List[Dict[str, Any]], **kwargs: Any) -> None:
        [message] = batch
        posted.append(message["event"])
        if message["event"] == EXPOSURE and posted.count(EXPOSURE) == 1:
            sending.set()
            finish.wait(5)
            if not delivered:
                raise RuntimeError("connection reset")

    try:
        with (
            _serving(_response(_split())),
            mock.patch("posthog.client.batch_post", side_effect=batch_post),
        ):
            first = threading.Thread(target=_read, args=(client, "get_feature_flag"))
            first.start()
            assert sending.wait(5)
            assert _read(client, "get_feature_flag") == "annual"
            finish.set()
            first.join(5)
            assert _read(client, "get_feature_flag") == "annual"
    finally:
        finish.set()
        client.shutdown()

    assert posted == [FLAG_CALLED, EXPOSURE] + ([] if delivered else [EXPOSURE])


def test_a_flags_reload_does_not_repeat_an_exposure(client):
    recording, events = _record_events(client)
    with _serving(_response(_split())), recording:
        _read(client, "get_feature_flag")
        client._update_flag_state(
            {"flags": [], "group_type_mapping": {}, "cohorts": {}}
        )
        _read(client, "get_feature_flag")
        client.evaluate_flags(DISTINCT_ID).get_flag(FLAG)

    assert len(_named(events, EXPOSURE)) == 1


# --- Super properties ------------------------------------------------------------------------

SPOOFED_SUPER_PROPERTIES = {
    "$feature_flag_config_version": 7,
    "$feature_flag_reason_code": "spoofed",
    "$feature_flag_rule_type": "spoofed",
    "$feature_flag_rule_id": OTHER_RULE,
    "$feature_flag_variant": "spoofed",
    "$feature_flag_reason": "spoofed",
    "$feature_flag_forced_variant": True,
    "$feature_flag_experiment_id": 301,
    "$experiment_exposure_source": "spoofed",
}


def _messages_with_super_properties(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []

    def before_send(message: Dict[str, Any]) -> Dict[str, Any]:
        messages.append(copy.deepcopy(message))
        return message

    client = Client(
        FAKE_TEST_API_KEY,
        send=False,
        before_send=before_send,
        super_properties=dict(SPOOFED_SUPER_PROPERTIES),
    )
    try:
        with _serving(_response(record)):
            _read(client, "get_feature_flag")
    finally:
        client.shutdown()
    return messages


def test_super_properties_cannot_change_config_version_2_attribution():
    messages = _messages_with_super_properties(_split())

    [called] = [m for m in messages if m["event"] == FLAG_CALLED]
    assert _attribution(called["properties"]) == EXPECTED_ATTRIBUTION
    for name in (
        "$feature_flag_reason",
        "$feature_flag_forced_variant",
        "$feature_flag_experiment_id",
    ):
        assert name not in called["properties"], name
    [exposure] = [m for m in messages if m["event"] == EXPOSURE]
    assert _attribution(exposure["properties"]) == _attribution(EXPECTED_EXPOSURE)
    assert not set(EXPOSURE_LEAVES_OUT) & set(exposure["properties"])
    for name in ("$feature_flag_experiment_id", "$feature_flag_forced_variant"):
        assert name not in exposure["properties"], name
    EXPOSURE_SCHEMA.validate(_projection(EXPOSURE_SCHEMA, exposure["properties"]))


def test_super_properties_cannot_add_rule_fields_a_config_version_2_call_omits():
    messages = _messages_with_super_properties(_record("no_rule_match", False))

    [called] = messages
    assert _attribution(called["properties"]) == {
        "$feature_flag_config_version": 2,
        "$feature_flag_reason_code": "no_rule_match",
    }


def test_super_properties_still_apply_to_config_version_1_calls():
    record = _record("condition_match", "annual", config_version=1)
    [called] = _messages_with_super_properties(record)

    for name, value in SPOOFED_SUPER_PROPERTIES.items():
        assert called["properties"][name] == value, name


async def test_async_super_properties_cannot_change_config_version_2_attribution():
    messages: List[Dict[str, Any]] = []
    with mock.patch(
        "posthog.async_client._async_flags",
        new=mock.AsyncMock(return_value=_response(_split())),
    ):
        client = AsyncPosthog(
            "project-key",
            send=False,
            super_properties=dict(SPOOFED_SUPER_PROPERTIES),
        )
        prepare = client._prepare_event

        def record(*args: Any, **kwargs: Any) -> Any:
            prepared, uuid = prepare(*args, **kwargs)
            messages.append(copy.deepcopy(prepared))
            return prepared, uuid

        with mock.patch.object(client, "_prepare_event", side_effect=record):
            snapshot = await client.evaluate_flags(DISTINCT_ID)
            snapshot.get_flag(FLAG)
        await client.shutdown()

    [called] = [m for m in messages if m["event"] == FLAG_CALLED]
    assert _attribution(called["properties"]) == EXPECTED_ATTRIBUTION
    assert "$feature_flag_reason" not in called["properties"]
    [exposure] = [m for m in messages if m["event"] == EXPOSURE]
    assert _attribution(exposure["properties"]) == _attribution(EXPECTED_EXPOSURE)
    assert not set(EXPOSURE_LEAVES_OUT) & set(exposure["properties"])
