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
import warnings
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional
from unittest import mock

import jsonschema
import pytest

from posthog import AsyncPosthog
from posthog.client import _MINIMAL_FLAG_CALLED_EVENT_PROPERTIES, Client
from posthog.test.test_flags_v4_response import FIXTURES, READERS, _build
from posthog.test.test_utils import FAKE_TEST_API_KEY

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


# --- The exact events of a split ---------------------------------------------------------


EXPECTED_EXPOSURE = {
    "$feature_flag": FLAG,
    "$feature_flag_response": "annual",
    "$feature_flag_config_version": 2,
    "$feature_flag_variant": "annual-first",
    "$feature_flag_rule_id": RULE,
    "$feature_flag_rule_type": "experiment",
    "$feature_flag_reason_code": "experiment_split",
    "$experiment_exposure_source": "sdk",
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
        "$feature_flag_reason": "Assigned variant 'annual-first'",
        "$feature_flag_version": 3,
        "$feature_flag_id": 77,
        "$feature_flag_has_experiment": True,
        "$feature_flag_config_version": 2,
        "$feature_flag_reason_code": "experiment_split",
        "$feature_flag_rule_id": RULE,
        "$feature_flag_rule_type": "experiment",
        "$feature_flag_variant": "annual-first",
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

    events = await _read_async_events([_response(_split())], [read])

    assert [event.name for event in events] == [FLAG_CALLED, EXPOSURE]
    called, exposure = events
    assert exposure.properties == EXPECTED_EXPOSURE
    assert _attribution(called.properties) == _attribution(EXPECTED_EXPOSURE)
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


def test_a_payload_lookup_for_another_value_sends_no_exposure(client):
    # The event reports the caller's match_value, which the record did not supply.
    recording, events = _record_events(client)
    with _serving(_response(_split())), recording, warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        client.get_feature_flag_payload(
            FLAG, DISTINCT_ID, match_value="monthly", send_feature_flag_events=True
        )

    [called] = events
    assert called.name == FLAG_CALLED
    assert called.properties["$feature_flag_response"] == "monthly"
    assert called.properties["$feature_flag_config_version"] == 2


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
    assert set(called["properties"]) <= _MINIMAL_FLAG_CALLED_EVENT_PROPERTIES
    assert "plan" not in called["properties"]
    assert _attribution(called["properties"]) == _attribution(EXPECTED_EXPOSURE)
    # The exposure is a full event.
    assert exposure["event"] == EXPOSURE
    assert exposure["properties"]["plan"] == "team"
    assert _attribution(exposure["properties"]) == _attribution(EXPECTED_EXPOSURE)
