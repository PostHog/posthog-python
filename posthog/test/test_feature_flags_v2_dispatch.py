"""Local evaluation chooses the evaluator of each flag by its config version.

A definition whose ``filters.version`` is absent or 1 keeps the version 1 evaluator. Any
other definition goes to the config version 2 evaluator. Its result is the record a
``/flags?v=4`` response gives for the same outcome, so the flag events are built as for
a remote record, with ``locally_evaluated`` true. A config version 2 flag that the
evaluator cannot decide falls back the way an inconclusive version 1 flag does: to a
remote request in the default mode, and to no value with ``only_evaluate_locally``.

The corpora, the local outcome mapping and the whole definitions feed come from
``fixtures/feature_flag_rules_v2``. Every other flag, identity and property here is
invented.
"""

from __future__ import annotations

import contextlib
import copy
import datetime
import hashlib
import json
import warnings
from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest import mock

import pytest
from freezegun import freeze_time

from posthog import _feature_flags_v2 as v2
from posthog.client import Client
from posthog.feature_flags import InconclusiveMatchError
from posthog.request import GetResponse
from posthog.test.test_experiment_exposure import (
    EXPOSURE,
    EXPOSURE_SCHEMA,
    FLAG_CALLED,
    FLAG_CALLED_SCHEMA,
    _attribution,
    _named,
    _projection,
    _record,
    _record_events,
    _response,
    _serving,
    _split,
)
from posthog.test.test_feature_flags_v2 import (
    REPLAYED,
    _as_expectation,
    _binary64,
    _local_outcome,
    _same_json,
)
from posthog.test.test_flags_v4_response import _load as _load_contract_file
from posthog.test.test_utils import FAKE_TEST_API_KEY
from posthog.types import (
    FeatureFlag,
    FeatureFlagResult,
    FlagMetadata,
    UnresolvedFlagReason,
    normalize_flags_response,
)

DISTINCT_ID = "reader-31"
# Drawn below 0.5 for the experiment seed, so a 50/50 split assigns the first variant.
SPLIT_SEED = "shelf-order"
# Drawn above 0.9 for the rollout seed, so a 50 percent rollout misses DISTINCT_ID.
MISSING_ROLLOUT_SEED = "reading-room"

RULE = "5c2e9a14-7b3d-4f61-a8e0-2d4c6b8f1e3a"
SECOND_RULE = "9e1f3b27-4c5a-4d8e-b6f2-7a0c1e3d5b49"
SPLIT_RULE = "c47d2e90-1a3b-4c5d-9e6f-8b0a2c4e6d1f"

UNSUPPORTED = UnresolvedFlagReason.UNSUPPORTED_DEFINITION
MISSING_CONTEXT = UnresolvedFlagReason.MISSING_CONTEXT


# --- Definitions -------------------------------------------------------------------


def _person(key: str, operator: str, value: Any) -> Dict[str, Any]:
    return {
        "key": key,
        "type": "person",
        "operator": operator,
        "value": value,
        "negation": False,
    }


def _targeted(
    value: Any, *, rule_id: str = RULE, properties: Tuple[Dict[str, Any], ...] = ()
) -> Dict[str, Any]:
    return {
        "id": rule_id,
        "rule_type": "targeted_release",
        "targeting": {"properties": list(properties)},
        "value": value,
    }


def _rollout(
    value: Any,
    percentage: float,
    *,
    rule_id: str = RULE,
    seed: str = MISSING_ROLLOUT_SEED,
    on_rollout_miss: str = "continue",
) -> Dict[str, Any]:
    return {
        "id": rule_id,
        "rule_type": "percentage_rollout",
        "targeting": {"properties": []},
        "value": value,
        "rollout_percentage": percentage,
        "on_rollout_miss": on_rollout_miss,
        "assignment_algorithm": "sha1_60_v1",
        "seed": seed,
        "assign_by": "person",
    }


def _experiment(
    *variants: Tuple[str, float, Any], rule_id: str = SPLIT_RULE
) -> Dict[str, Any]:
    return {
        "id": rule_id,
        "rule_type": "experiment",
        "targeting": {"properties": []},
        "paused": False,
        "rollout_percentage": 100,
        "on_rollout_miss": "continue",
        "assignment_algorithm": "sha1_60_v1",
        "seed": SPLIT_SEED,
        "assign_by": "person",
        "variants": [
            {"key": key, "weight": weight, "value": value}
            for key, weight, value in variants
        ],
    }


def _filters(
    *rules: Dict[str, Any], return_type: str = "boolean", default: Any = False
) -> Dict[str, Any]:
    return {
        "version": 2,
        "return_type": return_type,
        "default_value": default,
        "rules": list(rules),
    }


def _definition(
    key: str,
    filters: Any,
    *,
    active: bool = True,
    flag_id: int = 41,
    version: int = 6,
    has_experiment: bool = False,
) -> Dict[str, Any]:
    return {
        "id": flag_id,
        "name": f"Invented {key}",
        "key": key,
        "active": active,
        "filters": filters,
        "version": version,
        "has_experiment": has_experiment,
        "ensure_experience_continuity": False,
    }


def _v1_definition(
    key: str, *, variant: Optional[str] = None, **filters: Any
) -> Dict[str, Any]:
    v1_filters: Dict[str, Any] = {
        "groups": [{"properties": [], "rollout_percentage": 100}],
        **filters,
    }
    if variant is not None:
        v1_filters["multivariate"] = {
            "variants": [{"key": variant, "rollout_percentage": 100}]
        }
    return {"id": 12, "key": key, "active": True, "filters": v1_filters}


BOOLEAN = _definition("reading-mode", _filters(_targeted(True)))
STRING = _definition(
    "shelf-label",
    _filters(_targeted("compact"), return_type="string", default="standard"),
)
NUMBER = _definition(
    "page-size", _filters(_targeted(24), return_type="number", default=12)
)
OBJECT = _definition(
    "reader-theme",
    _filters(
        _targeted({"palette": "sepia", "sizes": [12, 14]}),
        return_type="object",
        default={"palette": "plain"},
    ),
)
SPLIT = _definition(
    "shelf-order",
    _filters(
        _experiment(("alphabetical", 50, "by-title"), ("recent", 50, "by-date")),
        return_type="string",
        default="by-title",
    ),
    has_experiment=True,
)
# Version 3 is a config version that no SDK reads.
UNKNOWN_VERSION = _definition(
    "future-format", {**_filters(_targeted(True)), "version": 3}
)
# The first rule matches everyone, but the parser is closed: the unknown field of the
# second rule rejects the whole document before any rule is evaluated.
UNKNOWN_RULE_FIELD = _definition(
    "closed-parser",
    _filters(
        _targeted(True),
        {**_targeted(True, rule_id=SECOND_RULE), "schedule": "weekdays"},
    ),
)
NEEDS_PLAN = _definition(
    "plan-gate",
    _filters(_targeted(True, properties=(_person("plan", "exact", "pro"),))),
)


@pytest.fixture
def client() -> Iterator[Client]:
    client = Client(FAKE_TEST_API_KEY, send=False, secret_key="secret-key")
    yield client
    client.shutdown()


def _load(
    client: Client, *definitions: Dict[str, Any], property_matching_version: int = 2
) -> None:
    """Install definitions the way a definitions response does."""
    client._update_flag_state(
        {
            "flags": copy.deepcopy(list(definitions)),
            "group_type_mapping": {"0": "company"},
            "cohorts": {},
            "property_matching_version": property_matching_version,
        }
    )


@contextlib.contextmanager
def _no_remote_request() -> Iterator[None]:
    with mock.patch(
        "posthog.client.flags", side_effect=AssertionError("unexpected /flags request")
    ) as remote:
        yield
    # The client catches the error of a failed request, so check after the reads.
    assert remote.call_count == 0, "unexpected /flags request"


def _result(client: Client, key: str, **kwargs: Any) -> Optional[FeatureFlagResult]:
    return client.get_feature_flag_result(key, DISTINCT_ID, **kwargs)


def _legacy(client: Client, method: str, key: str, **kwargs: Any) -> Any:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return getattr(client, method)(key, DISTINCT_ID, **kwargs)


# --- Which evaluator ----------------------------------------------------------------


@pytest.mark.parametrize(
    "filters",
    [
        pytest.param({"groups": [{"properties": [], "rollout_percentage": 100}]}),
        pytest.param(
            {"version": 1, "groups": [{"properties": [], "rollout_percentage": 100}]}
        ),
        pytest.param(
            {"version": 1.0, "groups": [{"properties": [], "rollout_percentage": 100}]}
        ),
    ],
    ids=["absent", "one", "one_as_float"],
)
def test_a_config_version_1_flag_keeps_the_version_1_evaluator(client, filters):
    with (
        mock.patch.object(v2, "_evaluate", side_effect=AssertionError("v2")),
        mock.patch.object(v2, "_parse_config", side_effect=AssertionError("v2")),
        _no_remote_request(),
    ):
        _load(client, _definition("legacy-shelf", filters))
        assert _legacy(client, "get_feature_flag", "legacy-shelf") is True
        snapshot = client.evaluate_flags(DISTINCT_ID)
        assert snapshot.get_flag("legacy-shelf") is True


@pytest.mark.parametrize("version", [2, 2.0, 3, None, True, "1", "2"], ids=repr)
def test_any_other_config_version_goes_to_the_config_version_2_evaluator(
    client, version
):
    filters = {**_filters(_targeted(True)), "version": version}
    _load(client, _definition("other-version", filters))

    with (
        mock.patch.object(v2, "_evaluate", wraps=v2._evaluate) as evaluate,
        _serving(_response(_record("targeting_match", "remote", key="other-version"))),
    ):
        result = _result(client, "other-version")

    assert evaluate.call_count == 1
    if version == 2:
        assert result is not None and result.get_value() is True
    else:
        # Only version 2 parses. Every other version is unsupported and falls back.
        assert result is not None and result.get_value() == "remote"


def test_every_definitions_request_asks_for_the_version_2_feed(client):
    # The first load and every poll select the feed that carries config version 2
    # definitions, including a poll that sends the ETag of that feed and gets 304.
    feed = copy.deepcopy(FEED["feed"])
    with mock.patch(
        "posthog.client.get",
        side_effect=[
            GetResponse(data=feed, etag='"feed-v2"'),
            GetResponse(data=None, etag='"feed-v2"', not_modified=True),
            GetResponse(data=feed, etag='"feed-v2-next"'),
        ],
    ) as get:
        client.load_feature_flags()
        client.load_feature_flags()
        # The 304 kept the config version 2 definitions the first load installed.
        assert "layout-preview" in (client.feature_flags_by_key or {})
        client.load_feature_flags()

    expected = f"/flags/definitions?token={FAKE_TEST_API_KEY}&send_cohorts&v=2"
    assert [request.args[1] for request in get.call_args_list] == [expected] * 3
    assert [request.kwargs["etag"] for request in get.call_args_list] == [
        None,
        '"feed-v2"',
        '"feed-v2"',
    ]


# --- Results ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "definition, value, payload",
    [
        (BOOLEAN, True, None),
        (STRING, "compact", None),
        (NUMBER, True, 24),
        (OBJECT, True, {"palette": "sepia", "sizes": [12, 14]}),
    ],
    ids=["boolean", "string", "number", "object"],
)
def test_a_config_version_2_flag_is_evaluated_locally_on_every_read(
    client, definition, value, payload
):
    key = definition["key"]
    _load(client, _v1_definition("legacy-shelf"), definition)

    with _no_remote_request():
        result = _result(client, key)
        assert result is not None
        assert (result.get_value(), result.payload) == (value, payload)
        assert _legacy(client, "get_feature_flag", key) == value
        assert _legacy(client, "feature_enabled", key) is True
        assert _legacy(client, "get_feature_flag_payload", key) == payload
        assert client.get_all_flags(DISTINCT_ID) == {"legacy-shelf": True, key: value}
        payloads = client.get_all_flags_and_payloads(DISTINCT_ID)["featureFlagPayloads"]
        # The legacy bulk getter keeps payloads encoded, as for a remote record.
        assert payloads == (
            {} if payload is None else {key: json.dumps(payload, separators=(",", ":"))}
        )

        snapshot = client.evaluate_flags(DISTINCT_ID)
        assert snapshot.keys == ["legacy-shelf", key]
        assert snapshot.get_flag(key) == value
        assert snapshot.is_enabled(key) is True
        assert snapshot.get_flag_payload(key) == payload
        assert snapshot.unresolved_flags == {}


@pytest.mark.parametrize(
    "definition, properties, record",
    [
        (
            SPLIT,
            {},
            {
                "value": "by-title",
                "reason": {"code": "experiment_split", "condition_index": 0},
                "metadata": {
                    "has_experiment": True,
                    "rule_type": "experiment",
                    "rule_id": SPLIT_RULE,
                    "variant_key": "alphabetical",
                },
            },
        ),
        (
            NUMBER,
            {},
            {
                "value": 24,
                "reason": {"code": "targeting_match", "condition_index": 0},
                "metadata": {"rule_type": "targeted_release", "rule_id": RULE},
            },
        ),
        (
            NEEDS_PLAN,
            {"plan": "free"},
            {
                "value": False,
                "reason": {"code": "no_rule_match", "condition_index": None},
                "metadata": {},
            },
        ),
    ],
    ids=["split", "number", "no_rule_match"],
)
def test_the_local_record_is_the_record_a_v4_response_gives(
    client, definition, properties, record
):
    _load(client, definition)
    [loaded] = client.feature_flags or []
    local = client._compute_flag_locally(
        loaded, DISTINCT_ID, person_properties=properties
    )

    response = {
        "flags": {
            loaded["key"]: {
                "key": loaded["key"],
                "value": record["value"],
                "reason": {**record["reason"], "description": "Evaluated locally"},
                "metadata": {
                    "id": 41,
                    "version": 6,
                    "config_version": 2,
                    "description": None,
                    "payload": None,
                    "has_experiment": False,
                    **record["metadata"],
                },
            }
        }
    }
    remote = normalize_flags_response(response)["flags"][loaded["key"]]

    assert isinstance(local, FeatureFlag)
    assert vars(local) == vars(remote)
    assert vars(local.metadata) == vars(remote.metadata)


@pytest.mark.parametrize(
    "stored, read",
    [(41.0, 41), (41.5, None), ("41", None), (True, None)],
    ids=["integral_float", "fraction", "string", "boolean"],
)
def test_the_record_reads_the_id_and_version_as_the_v4_reader_does(
    client, stored, read
):
    # JSON has one number type, so the v4 reader reads 41.0 as 41. Any other value is
    # left out, and the flag is still evaluated locally.
    _load(client, {**BOOLEAN, "id": stored, "version": stored})
    [loaded] = client.feature_flags or []

    local = client._compute_flag_locally(loaded, DISTINCT_ID)

    assert isinstance(local, FeatureFlag) and local.enabled is True
    assert (local.metadata.id, local.metadata.version) == (read, read)


def test_a_null_value_reads_as_disabled(client):
    _load(
        client,
        _definition(
            "shelf-label",
            _filters(
                _targeted("compact", properties=(_person("plan", "exact", "pro"),)),
                return_type="string",
                default=None,
            ),
        ),
    )

    with _no_remote_request():
        result = _result(client, "shelf-label", person_properties={"plan": "free"})
        snapshot = client.evaluate_flags(
            DISTINCT_ID, person_properties={"plan": "free"}
        )

    assert result is not None and result.get_value() is False
    assert snapshot.get_flag("shelf-label") is False
    assert snapshot.is_enabled("shelf-label", True) is False


def test_an_object_value_is_a_copy(client):
    _load(client, OBJECT)

    with _no_remote_request():
        first = _result(client, "reader-theme")
        assert first is not None
        first.payload["palette"] = "changed"
        first.payload["sizes"].append(99)
        snapshot = client.evaluate_flags(DISTINCT_ID)
        snapshot.get_flag_payload("reader-theme")["palette"] = "changed too"
        # The record keeps the typed value for the typed getters to come.
        [loaded] = client.feature_flags or []
        record = client._compute_flag_locally(loaded, DISTINCT_ID)
        record._value["sizes"].append(99)
        second = _result(client, "reader-theme")

    assert second is not None
    assert second.payload == {"palette": "sepia", "sizes": [12, 14]}
    assert loaded["filters"] == OBJECT["filters"]
    assert client._parsed_v2_definitions["reader-theme"].config.rules[0].value == {
        "palette": "sepia",
        "sizes": [12, 14],
    }


@pytest.mark.parametrize("unsupported_too", [False, True])
def test_an_inactive_flag_is_disabled_without_evaluating_its_rules(
    client, unsupported_too
):
    filters = (
        UNKNOWN_RULE_FIELD["filters"] if unsupported_too else _filters(_targeted(True))
    )
    _load(client, _definition("paused-shelf", filters, active=False))

    recording, events = _record_events(client)
    with _no_remote_request(), recording:
        result = _result(client, "paused-shelf")
        snapshot = client.evaluate_flags(DISTINCT_ID, only_evaluate_locally=True)
        assert snapshot.get_flag("paused-shelf") is False

    assert result is not None and result.get_value() is False
    assert snapshot.unresolved_flags == {}
    assert [_attribution(event.properties) for event in events] == [
        {
            "$feature_flag_config_version": 2,
            "$feature_flag_reason_code": "flag_disabled",
        }
    ]


def test_experience_continuity_and_device_bucketing_do_not_apply(client):
    # The flags service evaluates a config version 2 flag with the person's distinct
    # ID only, so these version 1 settings do not stop a local result.
    definition = {
        **SPLIT,
        "ensure_experience_continuity": True,
        "bucketing_identifier": "device_id",
    }
    _load(client, definition)

    with _no_remote_request():
        result = _result(client, "shelf-order")

    assert result is not None and result.get_value() == "by-title"


def test_the_property_matching_version_selects_exact_matching(client):
    # Version 1 matching compares a filter of boolean-like strings by truthiness.
    definition = _definition(
        "opted-in",
        _filters(
            _targeted(True, properties=(_person("plan", "exact", ["true", "false"]),))
        ),
    )
    with _no_remote_request():
        _load(client, definition, property_matching_version=1)
        loose = _result(client, "opted-in", person_properties={"plan": "pro"})
        _load(client, definition, property_matching_version=2)
        explicit = _result(client, "opted-in", person_properties={"plan": "pro"})

    assert loose is not None and loose.get_value() is True
    assert explicit is not None and explicit.get_value() is False


def test_dates_are_read_in_utc(client):
    definition = _definition(
        "early-reader",
        _filters(
            _targeted(
                True,
                properties=(
                    _person("joined_at", "is_date_before", "2025-06-15 00:30:00"),
                ),
            )
        ),
    )
    _load(client, definition)
    with _no_remote_request():
        # In any timezone east of UTC, 00:15 UTC is after the boundary.
        before = _result(
            client,
            "early-reader",
            person_properties={"joined_at": "2025-06-15T00:15:00Z"},
        )
        after = _result(
            client,
            "early-reader",
            person_properties={"joined_at": "2025-06-15T00:45:00Z"},
        )

    assert before is not None and before.get_value() is True
    assert after is not None and after.get_value() is False


def test_the_distinct_id_is_read_as_a_string(client):
    definition = _definition(
        "numbered-reader",
        _filters(
            _rollout(
                True, 50, seed=MISSING_ROLLOUT_SEED, on_rollout_miss="return_default"
            )
        ),
    )
    _load(client, definition)

    with _no_remote_request():
        numeric = client.get_feature_flag_result("numbered-reader", 58)
        text = client.get_feature_flag_result("numbered-reader", "58")

    assert numeric is not None and text is not None
    assert numeric.get_value() is text.get_value() is True


def test_each_definition_is_parsed_once_when_it_loads(client):
    with mock.patch.object(v2, "_parse_config", wraps=v2._parse_config) as parse:
        _load(client, _v1_definition("legacy-shelf"), BOOLEAN, STRING, UNKNOWN_VERSION)
        assert parse.call_count == 3

        with _serving(_response(_record("targeting_match", True, key="future-format"))):
            for _ in range(3):
                _result(client, "reading-mode")
                client.evaluate_flags(DISTINCT_ID)
                client.get_all_flags(DISTINCT_ID)

    assert parse.call_count == 3


# --- Falling back ---------------------------------------------------------------------

DATE_GATE = _definition(
    "early-reader",
    _filters(
        _targeted(
            True, properties=(_person("joined_at", "is_date_before", "2025-06-15"),)
        )
    ),
)
NUMBER_GATE = _definition(
    "long-reads",
    _filters(_targeted(True, properties=(_person("pages", "gt", 300),))),
)
REGEX_GATE = _definition(
    "pattern-gate",
    _filters(_targeted(True, properties=(_person("email", "regex", "(unclosed"),))),
)

# Each definition, the person properties of the read, and the unresolved reason.
UNDECIDED = {
    "unknown_version": (UNKNOWN_VERSION, {}, UNSUPPORTED),
    "unknown_rule_field": (UNKNOWN_RULE_FIELD, {}, UNSUPPORTED),
    "unknown_rule_type": (
        _definition(
            "new-rule-type",
            _filters({**_targeted(True), "rule_type": "scheduled_release"}),
        ),
        {},
        UNSUPPORTED,
    ),
    "group_aggregation": (
        _definition(
            "company-shelf",
            {**_filters(_targeted(True)), "aggregation_group_type_index": 0},
        ),
        {},
        UNSUPPORTED,
    ),
    "missing_property": (NEEDS_PLAN, {}, MISSING_CONTEXT),
    "invalid_regex": (REGEX_GATE, {"email": "reader@example.com"}, UNSUPPORTED),
    "invalid_property": (NUMBER_GATE, {"pages": "many"}, MISSING_CONTEXT),
    "datetime_property": (
        DATE_GATE,
        {"joined_at": datetime.datetime(2025, 1, 2, tzinfo=datetime.timezone.utc)},
        MISSING_CONTEXT,
    ),
}


@pytest.mark.parametrize("case", list(UNDECIDED), ids=str)
def test_an_undecided_flag_falls_back_to_a_remote_request(client, case):
    definition, properties, _ = UNDECIDED[case]
    key = definition["key"]
    _load(client, _v1_definition("legacy-shelf"), definition)
    response = _response(_record("targeting_match", "from-remote", key=key))

    with _serving(response) as remote:
        result = _result(client, key, person_properties=properties)
        snapshot = client.evaluate_flags(DISTINCT_ID, person_properties=properties)

    assert result is not None and result.get_value() == "from-remote"
    assert remote.call_count == 2
    assert snapshot.get_flag(key) == "from-remote"
    assert snapshot.get_flag("legacy-shelf") is True
    assert snapshot.unresolved_flags == {}


@pytest.mark.parametrize("case", list(UNDECIDED), ids=str)
def test_an_undecided_flag_is_unresolved_when_evaluating_only_locally(client, case):
    definition, properties, reason = UNDECIDED[case]
    key = definition["key"]
    _load(client, _v1_definition("legacy-shelf"), definition)

    with _no_remote_request():
        result = _result(
            client, key, person_properties=properties, only_evaluate_locally=True
        )
        snapshot = client.evaluate_flags(
            DISTINCT_ID, person_properties=properties, only_evaluate_locally=True
        )
        flags = client.get_all_flags(
            DISTINCT_ID, person_properties=properties, only_evaluate_locally=True
        )

    assert result is None
    assert snapshot.keys == ["legacy-shelf"]
    assert snapshot.unresolved_flags == {key: reason}
    assert snapshot.get_flag(key) is None
    assert flags == {"legacy-shelf": True}


def test_the_no_request_check_sees_a_request_the_client_recovers_from(client):
    # The client catches the error of a failed /flags request, so the check must not
    # rely on that error reaching the test.
    _load(client, NEEDS_PLAN)

    with pytest.raises(AssertionError, match="unexpected /flags request"):
        with _no_remote_request():
            _result(client, "plan-gate")


def test_a_rule_that_needs_a_missing_property_ends_the_evaluation(client):
    # The second rule would match everyone, but a rule that matches is terminal, so
    # the evaluator cannot skip the first one.
    definition = _definition(
        "plan-gate",
        _filters(
            _targeted(True, properties=(_person("plan", "exact", "pro"),)),
            _targeted(True, rule_id=SECOND_RULE),
        ),
    )
    _load(client, definition)

    with _no_remote_request():
        snapshot = client.evaluate_flags(DISTINCT_ID, only_evaluate_locally=True)

    assert snapshot.unresolved_flags == {"plan-gate": MISSING_CONTEXT}


# --- The contract's definitions feed -------------------------------------------------

# One whole definitions feed: version 1 and config version 2 flags, active and
# inactive, a cohort flag, a group flag, and three entries that the parser does not
# support, each between supported ones. For each entry the contract states the
# evaluator that reads it, what the reader does with it in each mode, and the result of
# a config version 2 entry for the fixture's context.
FEED = _load_contract_file("definitions_feed.json")
FEED_ENTRIES = list(zip(FEED["feed"]["flags"], FEED["expectations"]))
FEED_CONTEXT = FEED["context"]


def _load_feed(client: Client) -> None:
    # The client never knows that a property map is complete, reads dates in UTC and
    # takes exact matching from the feed, so the fixture's context must be one it uses.
    assert FEED_CONTEXT["properties_complete"] is False
    assert FEED_CONTEXT["timezone"] == "UTC"
    assert FEED_CONTEXT["explicit_exact_matching"] is (
        FEED["feed"]["property_matching_version"] == 2
    )
    client._update_flag_state(copy.deepcopy(FEED["feed"]))


def _record_as_expectation(record: FeatureFlag) -> Dict[str, Any]:
    """A local record in the shape of a corpus ``expected`` member."""
    metadata = record.metadata
    assert record.reason is not None and isinstance(metadata, FlagMetadata)
    expectation: Dict[str, Any] = {
        "status": "success",
        "value": record._value,
        "reason": record.reason.code,
    }
    rule = {
        "id": metadata._rule_id,
        "rule_type": metadata._rule_type,
        "index": record.reason.condition_index,
    }
    # The evaluator sets all three rule fields or none of them.
    present = [field is not None for field in rule.values()]
    assert all(present) or not any(present), f"{record.key} has a partial rule {rule}"
    if all(present):
        expectation["rule"] = rule
    if metadata._variant_key is not None:
        expectation["variant"] = metadata._variant_key
    return expectation


@pytest.mark.parametrize(
    "entry, expectation", FEED_ENTRIES, ids=[entry["key"] for entry, _ in FEED_ENTRIES]
)
def test_each_feed_entry_gets_the_evaluator_and_result_the_contract_states(
    client, entry, expectation
):
    _load_feed(client)
    definition = (client.feature_flags_by_key or {})[entry["key"]]

    def read() -> Any:
        return client._compute_flag_locally(
            definition,
            FEED_CONTEXT["identifier"],
            person_properties=FEED_CONTEXT["properties"],
        )

    if expectation["reader"]["local_only"] == "unsupported":
        with pytest.raises(InconclusiveMatchError) as raised:
            read()
        assert raised.value.reason is UNSUPPORTED
    elif expectation["evaluator"] == "v1":
        with mock.patch.object(v2, "_evaluate", side_effect=AssertionError("v2")):
            assert not isinstance(read(), FeatureFlag)
    else:
        record = read()
        assert isinstance(record, FeatureFlag)
        assert _same_json(_record_as_expectation(record), expectation["expected"])


@pytest.mark.parametrize("mode", ["auto", "local_only"])
def test_only_the_feed_entries_the_reader_does_not_support_fall_back(client, mode):
    _load_feed(client)
    remote = _response(
        *(
            _record("targeting_match", "from-remote", key=entry["key"])
            for entry, _ in FEED_ENTRIES
        )
    )
    recording, events = _record_events(client)

    with _serving(remote) as served, recording:
        snapshot = client.evaluate_flags(
            FEED_CONTEXT["identifier"],
            person_properties=FEED_CONTEXT["properties"],
            only_evaluate_locally=mode == "local_only",
        )
        values = {
            entry["key"]: snapshot.get_flag(entry["key"]) for entry, _ in FEED_ENTRIES
        }

    outcomes = {
        expectation["key"]: expectation["reader"][mode]
        for expectation in FEED["expectations"]
    }
    assert served.call_count == (1 if mode == "auto" else 0)
    assert snapshot.unresolved_flags == {
        key: UNSUPPORTED
        for key, outcome in outcomes.items()
        if outcome == "unsupported"
    }
    for entry, expectation in FEED_ENTRIES:
        key, outcome = entry["key"], outcomes[entry["key"]]
        if outcome == "unsupported":
            assert values[key] is None
            continue
        [called] = _named(events, FLAG_CALLED, key)
        assert called.properties["locally_evaluated"] is (outcome == "local_evaluation")
        if outcome == "remote_fallback":
            assert values[key] == "from-remote"
        elif "expected" in expectation:
            # A sibling of an entry that falls back keeps its own result.
            value, payload = _legacy_rendering(expectation["expected"]["value"])
            assert values[key] == value
            assert _same_json(snapshot.get_flag_payload(key), payload)


# --- Flag dependencies -------------------------------------------------------------


def _depends_on(key: str, dependency: str, value: Any, *later: Dict[str, Any]):
    """A version 1 flag whose first condition depends on ``dependency``."""
    return {
        "id": 13,
        "key": key,
        "active": True,
        "filters": {
            "groups": [
                {
                    "properties": [
                        {
                            "key": dependency,
                            "type": "flag",
                            "value": value,
                            "operator": "flag_evaluates_to",
                            "dependency_chain": [dependency],
                        }
                    ],
                    "rollout_percentage": 100,
                    "variant": None,
                },
                *later,
            ],
        },
    }


def test_a_version_1_flag_never_evaluates_a_config_version_2_dependency(client):
    # Read as version 1, the dependency has no conditions and is false, so the first
    # condition would match. A later condition would also answer for the flag.
    dependent = _depends_on(
        "after-reading-mode",
        "reading-mode",
        False,
        {"properties": [], "rollout_percentage": 100},
    )
    dependency = _definition("reading-mode", _filters(_targeted(True)))
    _load(client, dependency, dependent)

    with _no_remote_request():
        snapshot = client.evaluate_flags(DISTINCT_ID, only_evaluate_locally=True)
        result = _result(client, "after-reading-mode", only_evaluate_locally=True)

    assert result is None
    assert snapshot.get_flag("reading-mode") is True
    assert snapshot.unresolved_flags == {"after-reading-mode": UNSUPPORTED}

    response = _response(_record("targeting_match", "remote", key="after-reading-mode"))
    with _serving(response):
        result = _result(client, "after-reading-mode")
    assert result is not None and result.get_value() == "remote"


def test_a_version_1_dependency_is_evaluated_as_before(client):
    _load(
        client,
        _v1_definition("legacy-shelf"),
        _depends_on("after-legacy-shelf", "legacy-shelf", True),
    )

    with _no_remote_request():
        result = _result(client, "after-legacy-shelf", only_evaluate_locally=True)

    assert result is not None and result.get_value() is True


def test_the_load_warning_names_a_config_version_2_dependency_and_document(client):
    with mock.patch.object(client.log, "warning") as warning:
        _load(
            client,
            BOOLEAN,
            _depends_on("after-reading-mode", "reading-mode", True),
            UNKNOWN_VERSION,
            _definition("paused-future", UNKNOWN_VERSION["filters"], active=False),
        )
        client._warn_unresolvable_flags()

    [message] = [call.args[0] for call in warning.call_args_list]
    assert "(reason: unsupported_definition): after-reading-mode, future-format." in (
        message
    )


# --- Events ----------------------------------------------------------------------------

EXPECTED_SPLIT_ATTRIBUTION = {
    "$feature_flag_config_version": 2,
    "$feature_flag_reason_code": "experiment_split",
    "$feature_flag_rule_id": SPLIT_RULE,
    "$feature_flag_rule_type": "experiment",
    "$feature_flag_variant_key": "alphabetical",
}


def _single_read(client: Client) -> None:
    _result(client, "shelf-order")


def _snapshot_read(client: Client) -> None:
    client.evaluate_flags(DISTINCT_ID).get_flag("shelf-order")


READS = {"single": _single_read, "snapshot": _snapshot_read}


@pytest.mark.parametrize("read", list(READS))
def test_a_local_split_sends_both_events_as_locally_evaluated(client, read):
    _load(client, SPLIT)

    recording, events = _record_events(client)
    with _no_remote_request(), recording:
        READS[read](client)

    assert [event.name for event in events] == [FLAG_CALLED, EXPOSURE]
    called, exposure = events
    assert called.properties == {
        "$feature_flag": "shelf-order",
        "$feature_flag_response": "by-title",
        "locally_evaluated": True,
        "$feature/shelf-order": "by-title",
        "$feature_flag_id": 41,
        "$feature_flag_version": 6,
        "$feature_flag_has_experiment": True,
        **EXPECTED_SPLIT_ATTRIBUTION,
    }
    assert exposure.properties == {
        "$feature_flag": "shelf-order",
        "$feature_flag_response": "by-title",
        "locally_evaluated": True,
        "$feature_flag_id": 41,
        "$feature_flag_version": 6,
        "$feature_flag_variant_key": "alphabetical",
        "$feature_flag_rule_id": SPLIT_RULE,
    }
    FLAG_CALLED_SCHEMA.validate(_projection(FLAG_CALLED_SCHEMA, called.properties))
    EXPOSURE_SCHEMA.validate(_projection(EXPOSURE_SCHEMA, exposure.properties))


@pytest.mark.parametrize("read", list(READS))
@pytest.mark.parametrize(
    "stored, sent",
    [
        (41.0, {"$feature_flag_id": 41, "$feature_flag_version": 41}),
        (41.5, {}),
        ("41", {}),
    ],
    ids=["integral_float", "fraction", "string"],
)
def test_the_events_send_the_id_and_version_of_the_local_record(
    client, read, stored, sent
):
    # Both reads send what the record holds: an integral float as an integer, and no
    # other value that is not an integer.
    _load(client, {**SPLIT, "id": stored, "version": stored})

    recording, events = _record_events(client)
    with _no_remote_request(), recording:
        READS[read](client)

    assert [event.name for event in events] == [FLAG_CALLED, EXPOSURE]
    for event in events:
        carried = {
            key: event.properties[key]
            for key in ("$feature_flag_id", "$feature_flag_version")
            if key in event.properties
        }
        # As JSON text, so that 41.0 differs from 41.
        assert json.dumps(carried, sort_keys=True) == json.dumps(sent, sort_keys=True)


@pytest.mark.parametrize("read", list(READS))
def test_a_local_targeting_match_names_its_rule_and_sends_no_exposure(client, read):
    _load(client, _definition("shelf-order", _filters(_targeted(True))))

    recording, events = _record_events(client)
    with _no_remote_request(), recording:
        READS[read](client)

    [called] = events
    assert called.name == FLAG_CALLED
    assert called.properties["locally_evaluated"] is True
    assert "$feature_flag_reason" not in called.properties
    assert _attribution(called.properties) == {
        "$feature_flag_config_version": 2,
        "$feature_flag_reason_code": "targeting_match",
        "$feature_flag_rule_id": RULE,
        "$feature_flag_rule_type": "targeted_release",
    }


def test_a_local_rollout_miss_names_its_rule(client):
    _load(
        client,
        _definition(
            "shelf-order",
            _filters(_rollout(True, 50, on_rollout_miss="return_default")),
        ),
    )

    recording, events = _record_events(client)
    with _no_remote_request(), recording:
        result = _result(client, "shelf-order")

    assert result is not None and result.get_value() is False
    [called] = events
    assert _attribution(called.properties) == {
        "$feature_flag_config_version": 2,
        "$feature_flag_reason_code": "rollout_miss",
        "$feature_flag_rule_id": RULE,
        "$feature_flag_rule_type": "percentage_rollout",
    }


def test_repeated_local_reads_send_each_event_once(client):
    _load(client, SPLIT)

    recording, events = _record_events(client)
    with _no_remote_request(), recording:
        for read in READS.values():
            read(client)
            read(client)

    assert [event.name for event in events] == [FLAG_CALLED, EXPOSURE]


def test_a_remote_read_of_the_same_assignment_sends_no_second_exposure(client):
    _load(client, SPLIT)
    remote = _split("by-title", "alphabetical", rule_id=SPLIT_RULE, key="shelf-order")

    recording, events = _record_events(client)
    with _serving(_response(remote)) as flags, recording:
        _result(client, "shelf-order")
        _load(client)
        _result(client, "shelf-order")

    # Neither event's dedupe identity includes where the result came from.
    assert flags.call_count == 1
    assert [event.name for event in events] == [FLAG_CALLED, EXPOSURE]
    assert [event.properties["locally_evaluated"] for event in events] == [True, True]


def test_opting_out_of_flag_events_suppresses_both_local_events(client):
    _load(client, SPLIT)

    recording, events = _record_events(client)
    with _no_remote_request(), recording:
        _result(client, "shelf-order", send_feature_flag_events=False)
        client.evaluate_flags(DISTINCT_ID).get_flag(
            "shelf-order", send_feature_flag_events=False
        )

    assert events == []


def test_a_payload_lookup_for_a_match_value_reports_no_attribution(client):
    _load(client, SPLIT)

    recording, events = _record_events(client)
    with _no_remote_request(), recording:
        _legacy(
            client,
            "get_feature_flag_payload",
            "shelf-order",
            match_value="by-date",
            send_feature_flag_events=True,
        )

    [called] = events
    assert called.properties["$feature_flag_response"] == "by-date"
    assert called.properties["locally_evaluated"] is True
    assert _attribution(called.properties) == {}


# --- The definition cache -------------------------------------------------------------


class _Provider:
    def __init__(self, stored: Optional[Dict[str, Any]] = None) -> None:
        self.stored = stored
        self.received: List[Dict[str, Any]] = []

    def should_fetch_flag_definitions(self) -> bool:
        return self.stored is None

    def get_flag_definitions(self) -> Optional[Dict[str, Any]]:
        return self.stored

    def on_flag_definitions_received(self, data: Dict[str, Any]) -> None:
        self.received.append(json.loads(json.dumps(data)))

    def shutdown(self) -> None:
        pass


def _fetch_into(provider: _Provider, flags: List[Dict[str, Any]]) -> Client:
    client = Client(
        FAKE_TEST_API_KEY,
        send=False,
        secret_key="secret-key",
        flag_definition_cache_provider=provider,
    )
    response = GetResponse(
        data={"flags": flags, "group_type_mapping": {}, "cohorts": {}}
    )
    with mock.patch("posthog.client.get", return_value=response):
        client._load_feature_flags()
    return client


def _load_from_cache(stored: Dict[str, Any]) -> Client:
    client = Client(
        FAKE_TEST_API_KEY,
        send=False,
        secret_key="secret-key",
        flag_definition_cache_provider=_Provider(stored),
    )
    with mock.patch("posthog.client.get") as get:
        client._load_feature_flags()
    assert get.call_count == 0
    return client


def test_the_cache_keeps_config_version_2_flags_out_of_flags():
    legacy = _v1_definition("legacy-shelf")
    provider = _Provider()
    client = _fetch_into(provider, [legacy, BOOLEAN, UNKNOWN_VERSION])
    client.shutdown()

    [stored] = provider.received
    # A release without the config version 2 evaluator reads only ``flags``.
    assert stored["flags"] == [legacy]
    assert stored["flags_v2"] == [BOOLEAN, UNKNOWN_VERSION]


def test_the_cache_data_of_version_1_flags_is_unchanged():
    legacy = _v1_definition("legacy-shelf")
    provider = _Provider()
    client = _fetch_into(provider, [legacy])
    client.shutdown()

    assert provider.received == [
        {
            "flags": [legacy],
            "group_type_mapping": {},
            "cohorts": {},
            "minimal_flag_called_events": False,
            "property_matching_version": 1,
        }
    ]


def test_flags_read_from_the_cache_include_config_version_2_flags():
    legacy = _v1_definition("legacy-shelf")
    writer_provider = _Provider()
    writer = _fetch_into(writer_provider, [legacy, BOOLEAN, UNKNOWN_VERSION])
    [stored] = writer_provider.received

    reader = _load_from_cache(stored)
    try:
        with _no_remote_request():
            snapshot = reader.evaluate_flags(DISTINCT_ID, only_evaluate_locally=True)
        assert {key: snapshot.get_flag(key) for key in snapshot.keys} == {
            "legacy-shelf": True,
            "reading-mode": True,
        }
        assert snapshot.unresolved_flags == {"future-format": UNSUPPORTED}
        # Both clients hold the same definitions, so they agree on their identity.
        assert (
            reader._flag_definition_fingerprint == writer._flag_definition_fingerprint
        )
    finally:
        writer.shutdown()
        reader.shutdown()


@pytest.mark.parametrize(
    "keys",
    [
        ("reading-mode", "legacy-shelf"),
        ("legacy-shelf", "reading-mode", "legacy-label"),
    ],
    ids=["config_version_2_first", "interleaved"],
)
def test_the_cache_keeps_the_identity_of_the_definitions(keys):
    # The cache stores each config version apart, so a reader loads the definitions
    # in another order than the API sent them. Both must still give them one
    # identity, or the reader rejects every flag result the writer cached.
    definitions = {
        "reading-mode": BOOLEAN,
        "legacy-shelf": _v1_definition("legacy-shelf"),
        "legacy-label": _v1_definition("legacy-label", variant="compact"),
    }
    writer_provider = _Provider()
    writer = _fetch_into(writer_provider, [definitions[key] for key in keys])
    [stored] = writer_provider.received
    reader = _load_from_cache(stored)
    try:
        assert (
            reader._flag_definition_fingerprint == writer._flag_definition_fingerprint
        )
    finally:
        writer.shutdown()
        reader.shutdown()


def test_the_identity_of_version_1_definitions_is_unchanged():
    # Releases without the config version 2 evaluator share a flag cache with this
    # one, so they must give version 1 definitions the same identity.
    data = {
        "flags": [_v1_definition("legacy-shelf"), _v1_definition("legacy-label")],
        "group_type_mapping": {},
        "cohorts": {},
    }
    encoded = json.dumps(
        {**data, "property_matching_version": 1},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )

    assert Client._hash_flag_definitions(data) == (
        hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    )


# --- The corpora through the client ---------------------------------------------------


def _draw_for(case: Dict[str, Any]) -> Any:
    """The hash function of a case: a white-box case replaces every hash."""
    replaced = case.get("white_box", {}).get("hash01_binary64_hex")
    if replaced is None:
        return v2._draw
    return lambda *_: _binary64(replaced)


def _reachable(case: Dict[str, Any]) -> bool:
    """Whether the client, which never knows that a property map is complete, can
    reproduce the case: the evaluator gives the corpus result with a partial map."""
    context = case["context"]
    properties = dict(context["properties"] or {})
    properties.setdefault("distinct_id", context["identifier"])
    outcome = v2._evaluate_with_hash(
        v2._parse_config(case["config"]),
        v2._Context(
            identifier=context["identifier"],
            properties=properties,
            properties_complete=False,
            explicit_exact_matching=context["explicit_exact_matching"],
            timezone=datetime.timezone.utc,
            now=datetime.datetime.fromisoformat(context["now"].replace("Z", "+00:00")),
        ),
        _draw_for(case),
        active=case["flag"]["active"],
    )
    return _same_json(_as_expectation(outcome), case["expected"])


def _legacy_rendering(value: Any) -> Tuple[Any, Any]:
    """The value a legacy getter returns and the payload, for a typed value."""
    if value is None or isinstance(value, bool):
        return bool(value), None
    if isinstance(value, str):
        return value or True, None
    return True, value


RULE_REASONS = {
    "targeting_match",
    "experiment_split",
    "rollout_miss",
    "experiment_paused",
    "holdout",
}

# The client reads dates in UTC, so a case set in another timezone is replayed only by
# the evaluator's own tests.
REPLAYABLE = [case for case in REPLAYED if case["context"]["timezone"] == "UTC"]


def _replay_through_client(case: Dict[str, Any]) -> Tuple[Any, Any, List[Any]]:
    context = case["context"]
    key = case["flag"]["key"]
    definition = _definition(key, case["config"], active=case["flag"]["active"])
    client = Client(FAKE_TEST_API_KEY, send=False, secret_key="secret-key")
    try:
        _load(
            client,
            definition,
            property_matching_version=2 if context["explicit_exact_matching"] else 1,
        )
        recording, events = _record_events(client)
        with (
            freeze_time(context["now"]),
            _no_remote_request(),
            recording,
            mock.patch.object(v2, "_draw", _draw_for(case)),
        ):
            result = client.get_feature_flag_result(
                key,
                context["identifier"],
                person_properties=context["properties"],
                only_evaluate_locally=True,
            )
            snapshot = None
            if context["identifier"]:
                snapshot = client.evaluate_flags(
                    context["identifier"],
                    person_properties=context["properties"],
                    only_evaluate_locally=True,
                )
                snapshot.get_flag(key)
    finally:
        client.shutdown()
    return result, snapshot, events


@pytest.mark.parametrize("case", REPLAYABLE, ids=lambda case: case["id"])
def test_a_corpus_case_gives_its_result_through_the_client(case):
    key = case["flag"]["key"]
    expected = case["expected"]
    result, snapshot, events = _replay_through_client(case)

    if expected["status"] == "success" and _reachable(case):
        value, payload = _legacy_rendering(expected["value"])
        if "legacy" in case:
            assert (value, payload) == (
                case["legacy"]["value"],
                case["legacy"]["payload"],
            )
        assert result is not None
        assert result.get_value() == value
        assert _same_json(result.payload, payload)
        if snapshot is not None:
            assert snapshot.get_flag(key) == value
            assert _same_json(snapshot.get_flag_payload(key), payload)

        attribution = {
            "$feature_flag_config_version": 2,
            "$feature_flag_reason_code": expected["reason"],
        }
        if expected["reason"] in RULE_REASONS:
            attribution["$feature_flag_rule_id"] = expected["rule"]["id"]
            attribution["$feature_flag_rule_type"] = expected["rule"]["rule_type"]
        if expected["reason"] == "experiment_split":
            attribution["$feature_flag_variant_key"] = expected["variant"]
        [called, *exposures] = _named(events, FLAG_CALLED, key) + _named(
            events, EXPOSURE, key
        )
        assert called.properties["locally_evaluated"] is True
        assert _attribution(called.properties) == attribution
        assert len(exposures) == (1 if expected["reason"] == "experiment_split" else 0)
        for exposure in exposures:
            assert exposure.properties["locally_evaluated"] is True
        return

    if expected["status"] == "success":
        # A complete property map with a key the read never passed: the client cannot
        # know the key is absent, so the rule is undecided.
        assert case["context"]["properties_complete"] is True
        reason = MISSING_CONTEXT
    else:
        local = _local_outcome(expected)
        assert local["local_evaluation"] == "inconclusive"
        reason = UnresolvedFlagReason(local["unresolved_reason"])
    assert result is None
    if snapshot is not None:
        assert snapshot.unresolved_flags == {key: reason}


def test_the_client_replay_covers_the_corpora():
    succeeding = [
        case for case in REPLAYABLE if case["expected"]["status"] == "success"
    ]
    reachable = [case for case in succeeding if _reachable(case)]
    other_timezone = [case for case in REPLAYED if case not in REPLAYABLE]
    # Results through the client; results of a complete property map that the client
    # cannot reproduce; errors and parse errors; cases in another timezone.
    assert (
        len(reachable),
        len(succeeding) - len(reachable),
        len(REPLAYABLE) - len(succeeding),
        len(other_timezone),
    ) == (210, 8, 36, 8)
