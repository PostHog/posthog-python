"""The evaluator for config version 2 flags, replayed against the evaluation corpora.

The corpora and hash vectors in ``fixtures/feature_flag_rules_v2`` are copied unchanged
from a ``PostHog/posthog-sdk-test-harness`` contract; ``SOURCE.json`` records which.
Each corpus case gives a configuration, a person and the exact expected result.
"""

from __future__ import annotations

import collections
import copy
import datetime
import struct
from typing import Any, Dict, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pytest

from posthog import _feature_flags_v2 as v2
from posthog.test.test_flags_v4_response import _load
from posthog.types import UnresolvedFlagReason

BOOLEAN_CORPUS = "v2_boolean_evaluation.json"
VALUE_CORPUS = "v2_value_evaluation.json"
EXPERIMENT_CORPUS = "v2_experiment_evaluation.json"
HASH_VECTORS = _load("hash_sha1_60_v1.json")

# An ``omitted`` row describes the remote request boundary: the flags service leaves an
# inactive, deleted or unrequested flag out of its response before it evaluates
# anything. The evaluator has no such boundary, so these rows are skipped.
OMITTED = "omitted"
SKIP_OMITTED = "the row describes the remote request boundary, not the evaluator"

# The kind of local outcome each corpus expectation stands for. The corpus states what
# the reference evaluator returns; a later contract release states the local outcome of
# each case itself, and then replaces this table.
LOCAL_OUTCOMES: Dict[Tuple[str, Optional[str]], type] = {
    ("success", None): v2._Evaluated,
    ("error", "missing_context"): v2._Inconclusive,
    ("error", "invalid_property"): v2._EvaluationError,
    ("error", "invalid_regex"): v2._EvaluationError,
    ("parse_error", "malformed"): v2._Unsupported,
    ("parse_error", "unsupported"): v2._Unsupported,
}


def _corpus_cases(name: str) -> Iterator[Dict[str, Any]]:
    """Each case of a corpus, with the corpus defaults for its flag and context applied."""
    corpus = _load(name)
    defaults = corpus.get("defaults", {})
    for case in corpus["cases"]:
        yield {
            **case,
            "corpus": name,
            "flag": case.get("flag", defaults.get("flag")),
            "context": {**defaults.get("context", {}), **case.get("context", {})},
        }


CASES = [
    case
    for name in (BOOLEAN_CORPUS, VALUE_CORPUS, EXPERIMENT_CORPUS)
    for case in _corpus_cases(name)
]
REPLAYED = [case for case in CASES if case["expected"]["status"] != OMITTED]
SKIPPED = [case for case in CASES if case["expected"]["status"] == OMITTED]


def _binary64(hexadecimal: str) -> float:
    return struct.unpack(">d", bytes.fromhex(hexadecimal))[0]


def _binary64_hex(value: float) -> str:
    return struct.pack(">d", value).hex()


def _context(raw: Dict[str, Any]) -> v2._Context:
    return v2._Context(
        identifier=raw["identifier"],
        properties=raw["properties"],
        properties_complete=raw["properties_complete"],
        explicit_exact_matching=raw["explicit_exact_matching"],
        timezone=ZoneInfo(raw["timezone"]),
        now=datetime.datetime.fromisoformat(raw["now"].replace("Z", "+00:00")),
    )


def _same_json(left: Any, right: Any) -> bool:
    """Equality of JSON values by type as well as value: ``1`` is not ``True``."""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _same_json(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(map(_same_json, left, right))
    numbers = (int, float)
    same_kind = type(left) is type(right) or (
        isinstance(left, numbers) and isinstance(right, numbers)
    )
    return same_kind and left == right


def _as_expectation(outcome: v2._Outcome) -> Dict[str, Any]:
    """An outcome in the shape of a corpus ``expected`` member, which is exhaustive."""
    if isinstance(outcome, v2._Evaluated):
        expectation: Dict[str, Any] = {
            "status": "success",
            "value": outcome.value,
            "reason": outcome.reason,
        }
        if outcome.rule_id is not None:
            expectation["rule"] = {
                "id": outcome.rule_id,
                "rule_type": outcome.rule_type,
                "index": outcome.rule_index,
            }
        if outcome.variant_key is not None:
            expectation["variant"] = outcome.variant_key
        return expectation
    if isinstance(outcome, v2._Unsupported):
        return {"status": "parse_error", "kind": outcome.kind}
    if isinstance(outcome, v2._Inconclusive):
        return {"status": "error", "error": outcome.reason.value}
    return {"status": "error", "error": outcome.kind}


def _replay(case: Dict[str, Any]) -> Tuple[v2._Outcome, List[Tuple[str, str, str]]]:
    """Evaluate a case. Returns the outcome and each hash drawn as (use, input, hash)."""
    drawn: List[Tuple[str, str, str]] = []
    replaced = case.get("white_box", {}).get("hash01_binary64_hex")

    def draw(use: str, seed: str, subject: str) -> float:
        if replaced is not None:
            return _binary64(replaced)
        hash01 = v2._draw(use, seed, subject)
        prefix = seed if use == "holdout" else seed + "."
        salt = "variant" if use == "variant" else ""
        drawn.append((use, prefix + subject + salt, _binary64_hex(hash01)))
        return hash01

    # The parser must not change the document it reads.
    document = copy.deepcopy(case["config"])
    config = v2._parse_config(document)
    assert document == case["config"]
    outcome = v2._evaluate_with_hash(
        config, _context(case["context"]), draw, active=case["flag"]["active"]
    )
    if replaced is None:
        # The hash seam and the evaluator's own hash agree.
        assert outcome == v2._evaluate(
            config, _context(case["context"]), active=case["flag"]["active"]
        )
    return outcome, drawn


@pytest.mark.parametrize(
    "case",
    REPLAYED
    + [
        pytest.param(case, marks=pytest.mark.skip(reason=SKIP_OMITTED))
        for case in SKIPPED
    ],
    ids=lambda case: case["id"],
)
def test_a_corpus_case_evaluates_to_its_exact_expected_result(case):
    expected = case["expected"]
    outcome, drawn = _replay(case)

    kind = expected.get("error") or expected.get("kind")
    assert type(outcome) is LOCAL_OUTCOMES[expected["status"], kind]
    assert _same_json(_as_expectation(outcome), expected), outcome

    evidence = case.get("hash_evidence")
    if isinstance(evidence, list):
        # The experiment corpus lists every hash an evaluation draws, in draw order.
        assert drawn == [
            (
                entry["use"],
                entry["prefix"] + entry["identifier"] + entry["salt"],
                entry["hash01_binary64_hex"],
            )
            for entry in evidence
        ]
    elif evidence is not None and drawn:
        # The boolean corpus records one rollout hash, also for a case that draws none.
        assert drawn == [
            (
                "rollout",
                bytes.fromhex(evidence["input_utf8_hex"]).decode("utf-8"),
                evidence["hash01_binary64_hex"],
            )
        ]


def test_the_replay_covers_every_corpus_case():
    statuses = collections.Counter(
        (case["corpus"], case["expected"]["status"]) for case in CASES
    )
    assert statuses == {
        (BOOLEAN_CORPUS, "success"): 121,
        (BOOLEAN_CORPUS, "error"): 9,
        (BOOLEAN_CORPUS, "parse_error"): 2,
        (BOOLEAN_CORPUS, OMITTED): 3,
        (VALUE_CORPUS, "success"): 38,
        (VALUE_CORPUS, "parse_error"): 11,
        (EXPERIMENT_CORPUS, "success"): 60,
        (EXPERIMENT_CORPUS, "parse_error"): 13,
    }
    assert sorted(case["id"] for case in SKIPPED) == [
        "v2_boolean.eligibility.deleted",
        "v2_boolean.eligibility.inactive",
        "v2_boolean.eligibility.request_filtered",
    ]
    families = collections.Counter(
        (case["corpus"], case["family"]) for case in REPLAYED
    )
    assert families == {
        (BOOLEAN_CORPUS, "ordering"): 18,
        (BOOLEAN_CORPUS, "properties"): 67,
        (BOOLEAN_CORPUS, "context"): 8,
        (BOOLEAN_CORPUS, "errors"): 6,
        (BOOLEAN_CORPUS, "hashing"): 21,
        (BOOLEAN_CORPUS, "white_box"): 10,
        (BOOLEAN_CORPUS, "parser"): 2,
        (VALUE_CORPUS, "ordering"): 27,
        (VALUE_CORPUS, "values"): 11,
        (VALUE_CORPUS, "parser"): 11,
        (EXPERIMENT_CORPUS, "ordering"): 20,
        (EXPERIMENT_CORPUS, "assignment"): 21,
        (EXPERIMENT_CORPUS, "values"): 9,
        (EXPERIMENT_CORPUS, "white_box"): 10,
        (EXPERIMENT_CORPUS, "parser"): 13,
    }


# --- Hash vectors --------------------------------------------------------------------


@pytest.mark.parametrize("vector", HASH_VECTORS["vectors"], ids=lambda v: v["id"])
def test_a_hash_vector_gives_its_exact_binary64_hash(vector):
    # The JSON reader must round the decimal literal correctly, or a boundary case moves.
    assert _binary64_hex(vector["hash01"]) == vector["hash01_binary64_hex"]

    hash01 = v2._hash01(vector["prefix"], vector["identifier"], vector["salt"])

    assert _binary64_hex(hash01) == vector["hash01_binary64_hex"]
    if "identifier_before_truncation" in vector:
        truncated = vector["identifier_before_truncation"][: v2._MAX_IDENTIFIER_LENGTH]
        assert truncated == vector["identifier"]
    for check in vector.get("rollout_checks", []):
        included = v2._in_rollout(check["rollout_percentage"], lambda: hash01)
        assert included is check["included"]
    for check in vector.get("holdout_checks", []):
        member = v2._in_rollout(check["exclusion_percentage"], lambda: hash01)
        assert member is check["member"]
    if "variant_check" in vector:
        variants = _variants(vector["variant_check"]["variants"])
        selected = v2._select_variant(hash01, variants)
        assert selected.key == vector["variant_check"]["selected"]


def _variants(raw: List[Dict[str, Any]]) -> Tuple[v2._Variant, ...]:
    # The vectors name a variant's weight ``rollout_percentage``, as version 1 does.
    return tuple(
        v2._Variant(variant["key"], variant["rollout_percentage"], True)
        for variant in raw
    )


@pytest.mark.parametrize(
    "vector", HASH_VECTORS["threshold_vectors"], ids=lambda v: v["id"]
)
def test_a_threshold_vector_is_included_as_stated(vector):
    def draw() -> float:
        assert not vector.get("short_circuit"), "100 percent must not hash"
        return vector["hash01"]

    included = v2._in_rollout(vector["rollout_percentage"], draw)

    assert included is vector["included"]


@pytest.mark.parametrize(
    "vector", HASH_VECTORS["variant_vectors"], ids=lambda v: v["id"]
)
def test_a_variant_vector_selects_the_stated_variant(vector):
    selected = v2._select_variant(vector["hash01"], _variants(vector["variants"]))

    # Past the last boundary version 1 selects nothing, and version 2 the last variant.
    assert selected.key == vector.get("v2_selected", vector["selected"])


@pytest.mark.parametrize(
    "parity",
    [parity for parity in HASH_VECTORS["seed_parity"] if "v2" in parity],
    ids=lambda parity: parity["id"],
)
def test_a_seed_equal_to_the_flag_key_hashes_the_version_1_input(parity):
    seeded = parity["v2"]
    use = {"": "rollout", "variant": "variant"}[seeded["salt"]]
    if seeded["prefix"] == seeded["seed"]:
        use = "holdout"

    hash01 = v2._draw(use, seeded["seed"], seeded["identifier"])

    assert seeded["prefix"] + seeded["identifier"] + seeded["salt"] == seeded["input"]
    assert hash01 == v2._hash01(seeded["prefix"], seeded["identifier"], seeded["salt"])
    assert hash01 == parity["hash01"]
    assert (seeded["input"] == parity["v1_input"]) is parity["identical_bytes"]


# --- What the corpora do not cover ---------------------------------------------------

RULE_ID = "7f3c2a10-8d4e-4b6a-9c21-5e0f1a2b3c4d"
OTHER_RULE_ID = "2b9d4e61-0c7a-4f35-8e12-6a4b3c2d1e0f"
NOW = datetime.datetime(2025, 6, 15, 12, 0, tzinfo=datetime.timezone.utc)
OSLO = ZoneInfo("Europe/Oslo")


def _targeted(*properties: Dict[str, Any], **fields: Any) -> Dict[str, Any]:
    return {
        "id": RULE_ID,
        "rule_type": "targeted_release",
        "targeting": {"properties": list(properties)},
        "value": True,
        **fields,
    }


def _experiment(**fields: Any) -> Dict[str, Any]:
    return {
        "id": RULE_ID,
        "rule_type": "experiment",
        "targeting": {"properties": []},
        "paused": False,
        "rollout_percentage": 100,
        "on_rollout_miss": "continue",
        "assignment_algorithm": "sha1_60_v1",
        "seed": "layout-test",
        "variants": [
            {"key": "control", "weight": 50, "value": False},
            {"key": "test", "weight": 50, "value": True},
        ],
        **fields,
    }


def _filters(*rules: Dict[str, Any], **fields: Any) -> Dict[str, Any]:
    return {
        "version": 2,
        "return_type": "boolean",
        "default_value": False,
        "rules": list(rules),
        **fields,
    }


def _person(key: str, operator: str, value: Any) -> Dict[str, Any]:
    return {"key": key, "type": "person", "operator": operator, "value": value}


def _nested(depth: int) -> Any:
    value: Any = 0
    for _ in range(depth):
        value = [value]
    return value


def _evaluate(
    filters: Any,
    properties: Optional[Dict[str, Any]] = None,
    *,
    identifier: str = "visitor-42",
    complete: bool = True,
    timezone: Optional[datetime.tzinfo] = OSLO,
    active: bool = True,
) -> v2._Outcome:
    context = v2._Context(
        identifier=identifier,
        properties={} if properties is None else properties,
        properties_complete=complete,
        explicit_exact_matching=True,
        timezone=timezone,
        now=NOW,
    )
    return v2._evaluate(v2._parse_config(filters), context, active=active)


def _match(
    operator: str, filter_value: Any, property_value: Any, **options: Any
) -> Any:
    """Whether a person whose ``plan`` is ``property_value`` matches the filter.

    ``True`` or ``False`` for a decided filter. Otherwise the name of what ended the
    evaluation: the reason of an inconclusive result, or the kind of an error or of a
    rejected document.
    """
    filters = _filters(_targeted(_person("plan", operator, filter_value)))
    outcome = _evaluate(filters, {"plan": property_value}, **options)
    if isinstance(outcome, v2._Evaluated):
        return outcome.reason == "targeting_match"
    if isinstance(outcome, v2._Inconclusive):
        return outcome.reason.value
    return outcome.kind


def _document_with(path: List[Any], value: Any) -> Dict[str, Any]:
    """A valid document that uses every part of the format, with one member set."""
    document = _filters(
        _experiment(
            targeting={"properties": [_person("plan", "exact", "pro")]},
            holdout={"seed": "layout-holdout", "exclusion_percentage": 5},
        )
    )
    target: Any = document
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    return document


RULE = ["rules", 0]
PROPERTY = RULE + ["targeting", "properties", 0]
VARIANT = RULE + ["variants", 0]
HOLDOUT = RULE + ["holdout"]


def test_the_document_the_parser_cases_change_is_valid():
    assert isinstance(
        v2._parse_config(_document_with(["default_value"], True)), v2._Config
    )


@pytest.mark.parametrize(
    "path, value, kind",
    [
        # An unknown field at any level.
        (["unknown"], 1, "malformed"),
        (RULE + ["unknown"], 1, "malformed"),
        (RULE + ["targeting", "unknown"], 1, "malformed"),
        (PROPERTY + ["unknown"], 1, "malformed"),
        (VARIANT + ["unknown"], 1, "malformed"),
        (HOLDOUT + ["unknown"], 1, "malformed"),
        # A field of config version 1, or of another rule type.
        (["groups"], [], "malformed"),
        (RULE + ["value"], True, "malformed"),
        # An unknown literal.
        (["return_type"], "integer", "malformed"),
        (RULE + ["rule_type"], "variant_rollout", "malformed"),
        (RULE + ["assignment_algorithm"], "murmur3", "malformed"),
        (RULE + ["on_rollout_miss"], "stop", "malformed"),
        (RULE + ["assign_by"], "group", "malformed"),
        (PROPERTY + ["operator"], "is_like", "malformed"),
        (PROPERTY + ["operator"], "in", "malformed"),
        (PROPERTY + ["operator"], "min", "malformed"),
        (PROPERTY + ["type"], "event", "malformed"),
        # A part of the format this evaluator does not read.
        (PROPERTY + ["type"], "cohort", "unsupported"),
        (PROPERTY + ["type"], "group", "unsupported"),
        (PROPERTY + ["type"], "flag", "unsupported"),
        (["aggregation_group_type_index"], 0, "unsupported"),
        (["aggregation_group_type_index"], 0.5, "malformed"),
        # A config version that is not the number 2.
        (["version"], 3, "unsupported"),
        (["version"], 1, "unsupported"),
        (["version"], "2", "unsupported"),
        (["version"], True, "unsupported"),
        (["version"], None, "unsupported"),
        # A wrong type, or a value out of range.
        (["rules"], {}, "malformed"),
        (RULE + ["id"], RULE_ID.replace("-", ""), "malformed"),
        (RULE + ["paused"], 0, "malformed"),
        (RULE + ["rollout_percentage"], True, "malformed"),
        (RULE + ["rollout_percentage"], 100.5, "malformed"),
        (RULE + ["rollout_percentage"], 12.345, "malformed"),
        (RULE + ["rollout_percentage"], float("nan"), "malformed"),
        (RULE + ["seed"], "", "malformed"),
        (RULE + ["seed"], "s" * 401, "malformed"),
        (RULE + ["seed"], "\ud800", "malformed"),
        (RULE + ["description"], None, "malformed"),
        (RULE + ["metadata"], [], "malformed"),
        (RULE + ["metadata"], {"ratio": float("inf")}, "malformed"),
        (RULE + ["metadata"], {"count": 10**400}, "malformed"),
        (RULE + ["metadata"], {"when": NOW}, "malformed"),
        (RULE + ["metadata"], {"nested": _nested(200)}, "malformed"),
        (RULE + ["holdout"], None, "malformed"),
        (RULE + ["variants"], [], "malformed"),
        (VARIANT + ["key"], "con trol", "malformed"),
        (VARIANT + ["value"], 1, "malformed"),
        (PROPERTY + ["key"], "", "malformed"),
        (PROPERTY + ["negation"], "yes", "malformed"),
        (PROPERTY + ["group_type_index"], 0, "malformed"),
        (PROPERTY + ["label"], 7, "malformed"),
        (PROPERTY + ["group_key_names"], {"0": 1}, "malformed"),
    ],
)
def test_the_parser_rejects_the_whole_document(path, value, kind):
    outcome = _evaluate(_document_with(path, value), {"plan": "pro"})

    assert isinstance(outcome, v2._Unsupported)
    assert outcome.kind == kind


@pytest.mark.parametrize(
    "path, value",
    [
        (["version"], 2.0),
        (RULE + ["description"], "Layout test"),
        (RULE + ["metadata"], {"owner": {"team": ["growth"]}, "nested": _nested(50)}),
        (RULE + ["assign_by"], "person"),
        (RULE + ["rollout_percentage"], 33.33),
        (PROPERTY + ["negation"], None),
        (PROPERTY + ["group_type_index"], None),
        (PROPERTY + ["label"], "Plan"),
        (PROPERTY + ["cohort_name"], None),
        (PROPERTY + ["group_key_names"], {"0": "Acme"}),
    ],
)
def test_the_parser_reads_every_optional_member(path, value):
    assert isinstance(v2._parse_config(_document_with(path, value)), v2._Config)


@pytest.mark.parametrize("filters", [None, [], "{}", 2])
def test_a_document_that_is_not_an_object_is_malformed(filters):
    assert v2._parse_config(filters) == v2._Unsupported("malformed", "filters")


def test_rule_ids_are_unique_whatever_their_letter_case():
    upper = _targeted(id=RULE_ID.upper())

    duplicate = v2._parse_config(_filters(_targeted(), upper))
    outcome = _evaluate(_filters(upper))

    assert duplicate == v2._Unsupported("malformed", "rule.id")
    assert isinstance(outcome, v2._Evaluated) and outcome.rule_id == RULE_ID


@pytest.mark.parametrize(
    "field, document",
    [
        ("rules", _filters(*[_targeted()] * 101)),
        ("properties", _filters(_targeted(*[_person("plan", "is_set", None)] * 101))),
        ("variants", _filters(_experiment(variants=[{}] * 21))),
    ],
)
def test_a_document_over_a_count_limit_is_malformed(field, document):
    assert v2._parse_config(document) == v2._Unsupported("malformed", field)


@pytest.mark.parametrize(
    "operator, value",
    [
        ("regex", 5),
        ("icontains", ["a"]),
        ("icontains_multi", "a"),
        ("gt", True),
        ("gt", None),
        ("semver_eq", 3),
        ("semver_eq", "v1.2.3"),
        ("semver_eq", "1.2.3+build"),
        ("semver_eq", "1.0.0-rc.01"),
        ("semver_eq", "1.x"),
        ("semver_eq", "18446744073709551616.0.0"),
        ("semver_wildcard", "*"),
        ("semver_wildcard", "1.*.3"),
        ("is_date_after", 1704067200),
    ],
)
def test_a_filter_value_its_operator_cannot_use_is_malformed(operator, value):
    assert _match(operator, value, "pro") == "malformed"


@pytest.mark.parametrize(
    "value",
    [
        "2025-06-15T12:00:00",
        "2025-06-15 12:00:00 UTC",
        "2025-06-15T12:00:00+25:00",
        "2025-02-30",
        "June 15, 2025",
        "1704067200",
        "10000d",
        "3x",
    ],
)
def test_a_date_filter_the_evaluator_cannot_resolve_is_unsupported(value):
    assert _match("is_date_after", value, "2025-06-15") == "unsupported"


def test_an_absent_or_null_operator_is_exact():
    absent = {"key": "plan", "type": "person", "value": "pro"}

    for prop in (absent, {**absent, "operator": None}):
        outcome = _evaluate(_filters(_targeted(prop)), {"plan": "PRO"})

        assert isinstance(outcome, v2._Evaluated)
        assert outcome.reason == "targeting_match"


def test_an_inactive_flag_has_no_value_whatever_its_document_holds():
    disabled = v2._Evaluated(None, "flag_disabled")

    assert _evaluate(_filters(_targeted()), active=False) == disabled
    assert _evaluate(_filters(version=3), active=False) == disabled


def test_an_inconclusive_rule_stops_the_evaluation_before_a_later_rule():
    first = _targeted(_person("plan", "exact", "pro"))
    filters = _filters(first, _targeted(id=OTHER_RULE_ID))

    outcome = _evaluate(filters, {"country": "NO"}, complete=False)

    assert isinstance(outcome, v2._Inconclusive)
    assert outcome.reason is UnresolvedFlagReason.MISSING_CONTEXT


def test_an_earlier_terminal_rule_hides_a_later_rule_that_cannot_be_decided():
    later = _targeted(_person("plan", "exact", "pro"), id=OTHER_RULE_ID)

    outcome = _evaluate(_filters(_targeted(), later), complete=False)

    assert outcome == v2._Evaluated(
        True, "targeting_match", RULE_ID, "targeted_release", 0
    )


def test_an_identifier_that_is_not_unicode_text_is_inconclusive_when_hashed():
    hashed = _evaluate(
        _filters(_experiment(rollout_percentage=50)), identifier="\ud800"
    )
    not_hashed = _evaluate(_filters(_targeted()), identifier="\ud800")

    assert isinstance(hashed, v2._Inconclusive)
    assert hashed.reason is UnresolvedFlagReason.MISSING_CONTEXT
    assert isinstance(not_hashed, v2._Evaluated)


def test_an_object_value_is_returned_as_configured():
    layout = {"columns": 2, "panels": [{"id": "a", "ratio": 0.5}], "title": None}
    filters = _filters(
        _targeted(value=layout), return_type="object", default_value=None
    )

    outcome = _evaluate(filters)

    assert isinstance(outcome, v2._Evaluated) and outcome.value == layout


@pytest.mark.parametrize(
    "operator, filter_value, property_value, expected",
    [
        # A property value is compared as the JSON text the flags service sees.
        ("exact", "null", None, True),
        ("exact", ["1.5", "2"], 1.5, True),
        ("is_not", "true", True, False),
        ("icontains", "ul", None, True),
        ("icontains_multi", ["x", "RO"], "pro", True),
        ("not_icontains_multi", ["x", "RO"], "pro", False),
        ("icontains_multi", [], "pro", False),
        ("icontains_multi", [1, True], "plan-1", True),
        ("regex", "^nu", None, True),
        ("regex", "", "anything", True),
        # Without a filter value nothing matches, also for is_not.
        ("exact", None, "pro", False),
        ("is_not", None, "pro", False),
    ],
)
def test_a_text_operator_compares_json_text(
    operator, filter_value, property_value, expected
):
    assert _match(operator, filter_value, property_value) == expected


FILTER_VALUES = {
    "icontains_multi": ["pro"],
    "not_icontains_multi": ["pro"],
    "is_date_exact": "2025-06-15",
    "is_date_after": "2025-06-15",
    "is_date_before": "2025-06-15",
}


@pytest.mark.parametrize("operator", sorted(v2._OPERATORS))
def test_only_a_negative_operator_matches_a_person_without_the_property(operator):
    filter_value = FILTER_VALUES.get(operator, "1.2.3")
    filters = _filters(_targeted(_person("plan", operator, filter_value)))

    outcome = _evaluate(filters, {})

    negative = operator in v2._OPERATORS_MATCHING_ABSENCE or operator == "is_not_set"
    assert isinstance(outcome, v2._Evaluated)
    assert (outcome.reason == "targeting_match") is negative


def test_an_invalid_pattern_is_an_error_for_a_person_without_the_property():
    filters = _filters(_targeted(_person("plan", "not_regex", "[")))

    assert _evaluate(filters, {}) == v2._EvaluationError(
        "invalid_regex", "The pattern for 'plan' is not valid"
    )


def test_a_pattern_python_cannot_run_like_the_flags_service_is_inconclusive():
    assert _match("regex", r"(?<=pre)mium", "premium") == "unsupported_definition"


@pytest.mark.parametrize(
    "operator, filter_value, property_value, expected",
    [
        ("gt", "5", "6", True),
        ("gt", 5, "6.0e0", True),
        ("lte", 0.1, ".1", True),
        ("lt", 5, "-inf", True),
        ("gt", 5, "Infinity", True),
        ("gte", 5, "nan", False),
        ("lte", 5, "NaN", False),
        ("gt", 5, True, "invalid_property"),
        ("gt", 5, "six", "invalid_property"),
        ("gt", 5, " 6", "invalid_property"),
        ("gt", 5, "6_0", "invalid_property"),
        ("gt", 5, "0x10", "invalid_property"),
        ("gt", 5, [6], "invalid_property"),
        ("gt", "five", 6, "invalid_property"),
    ],
)
def test_a_number_operator_reads_numbers_like_the_flags_service(
    operator, filter_value, property_value, expected
):
    assert _match(operator, filter_value, property_value) == expected


@pytest.mark.parametrize(
    "operator, filter_value, property_value",
    [
        ("gt", 5, float("nan")),
        ("gt", 5, float("inf")),
        ("gt", 5, 10**400),
        ("gt", 5, NOW),
        ("is_date_after", "2025-06-15", float("nan")),
        ("is_date_after", "2025-06-15", NOW),
        ("semver_gt", "1.2.3", float("inf")),
        ("exact", "pro", NOW),
        ("icontains", "pro", float("nan")),
        ("icontains_multi", ["pro"], NOW),
        ("regex", "pro", NOW),
    ],
)
def test_a_property_value_json_cannot_carry_is_inconclusive(
    operator, filter_value, property_value
):
    assert _match(operator, filter_value, property_value) == "missing_context"


@pytest.mark.parametrize(
    "operator, filter_value, property_value, expected",
    [
        # Leading zeros, a missing patch number and one leading "v" are read.
        ("semver_eq", "3.8.0", "v03.08", True),
        ("semver_eq", "3.8.0", 3.8, True),
        ("semver_eq", "3.8.0", " 3.8.0\n", True),
        ("semver_eq", "3.8.0", "V3.8.0", False),
        ("semver_eq", "3.8.0", "3.8.0.1", False),
        ("semver_neq", "3.8.0", "not a version", False),
        # A pre-release sorts below its release, and its parts sort one by one.
        ("semver_lt", "1.0.0", "1.0.0-rc.1", True),
        ("semver_gt", "1.0.0-alpha", "1.0.0-alpha.1", True),
        ("semver_gt", "1.0.0-alpha.9", "1.0.0-alpha.10", True),
        ("semver_gt", "1.0.0-1", "1.0.0-alpha", True),
        ("semver_eq", "1.0.0-rc.1", "1.0.0-rc.1", True),
        # Build metadata takes part in the comparison.
        ("semver_eq", "1.0.0", "1.0.0+build.5", False),
        ("semver_gt", "1.0.0", "1.0.0+build.5", True),
        ("semver_gt", "1.0.0-rc+7", "1.0.0-rc+10", True),
        # A tilde range keeps the minor number.
        ("semver_tilde", "1.2.3", "1.2.9", True),
        ("semver_tilde", "1.2.3", "1.2.2", False),
        ("semver_tilde", "1.2.3", "1.3.0", False),
        ("semver_tilde", "1.2", "1.2.0", True),
        # A caret range keeps the leftmost number that is not zero.
        ("semver_caret", "1.2.3", "1.9.0", True),
        ("semver_caret", "1.2.3", "1.2.2", False),
        ("semver_caret", "1.2.3", "2.0.0", False),
        ("semver_caret", "0.2.3", "0.2.9", True),
        ("semver_caret", "0.2.3", "0.3.0", False),
        ("semver_caret", "0.0.3", "0.0.3", True),
        ("semver_caret", "0.0.3", "0.0.4", False),
        # A range matches a pre-release only of the version it names one for.
        ("semver_caret", "1.2.3", "1.3.0-rc.1", False),
        ("semver_caret", "1.2.3-rc.1", "1.2.3-rc.2", True),
        ("semver_caret", "1.2.3-rc.2", "1.2.3-rc.1", False),
        ("semver_caret", "1.2.3-rc.1", "1.2.4-rc.1", False),
        ("semver_tilde", "1.2.3-rc.1", "1.2.3", True),
        # A wildcard fixes the numbers before the first "*".
        ("semver_wildcard", "1.*", "1.9.9", True),
        ("semver_wildcard", "1.*", "2.0.0", False),
        ("semver_wildcard", "1.*.*", "1.0.0", True),
        ("semver_wildcard", "01.02.*", "1.2.7", True),
        ("semver_wildcard", "1.2.*", "1.3.0", False),
        ("semver_wildcard", "1.2.*", "1.2.3-rc.1", False),
        # Without a "*" the value reads as a caret range.
        ("semver_wildcard", "1.2.3", "1.4.0", True),
        ("semver_wildcard", "1.2", "1.1.0", False),
        ("semver_wildcard", "0.2", "0.3.0", False),
        ("semver_wildcard", "1", "1.0.0", True),
        # The format admits these values, but they are not ranges.
        ("semver_wildcard", "1.2.3.*", "1.2.3", "invalid_property"),
        ("semver_wildcard", "1.2-rc.*", "1.2.0", "invalid_property"),
        # After a pre-release the "*" is not a wildcard but the letter "x".
        ("semver_wildcard", "1.2.3-rc.*", "1.2.3-rc.x", True),
        ("semver_wildcard", "1.2.3-rc.*", "1.2.3-rc.w", False),
        ("semver_wildcard", "1.2.3-rc.*", "1.4.0", True),
        ("semver_wildcard", "1.2.3.*", "not a version", False),
    ],
)
def test_a_semver_operator_orders_versions_like_the_flags_service(
    operator, filter_value, property_value, expected
):
    assert _match(operator, filter_value, property_value) == expected


@pytest.mark.parametrize(
    "operator, filter_value, property_value, expected",
    [
        # Nanoseconds are compared.
        (
            "is_date_after",
            "2025-06-15T12:00:00Z",
            "2025-06-15T12:00:00.000000001Z",
            True,
        ),
        ("is_date_exact", "2025-06-15T12:00:00.5Z", "2025-06-15T12:00:00.500Z", True),
        # A date and time without an offset is a wall clock in the project's timezone.
        ("is_date_exact", "2025-06-15 14:00", "2025-06-15T12:00:00Z", True),
        ("is_date_exact", "2025-06-15 14:00:00.25", "2025-06-15T14:00:00.250", True),
        ("is_date_exact", "2025-06-15", "2025-06-14T22:00:00+00:00", True),
        ("is_date_exact", "2025-06-15T12:00:00-03:30", "2025-06-15T15:30:00Z", True),
        # A number is Unix epoch seconds. The fraction of a negative number is dropped.
        ("is_date_exact", "2025-06-15T12:00:00.5Z", 1749988800.5, True),
        ("is_date_exact", "1969-12-31T23:59:58Z", -1.5, True),
        ("is_date_exact", "1970-01-01T00:00:00Z", "0", True),
        ("is_date_before", "2025-06-15", "1e9", True),
        # A property can hold a relative date too.
        ("is_date_exact", "-1d", "1d", True),
        ("is_date_exact", "-2m", "2025-04-15T12:00:00Z", True),
        # A wall clock in a spring-forward gap is no instant.
        ("is_date_exact", "2025-03-30 02:30:00", "2025-03-30T02:30:00", False),
        ("is_date_before", "2026-01-01", "2025-03-30T02:30:00", False),
        # A wall clock that repeats means its first instant.
        ("is_date_exact", "2025-10-26 02:30:00", "2025-10-26T00:30:00Z", True),
        # Not a date at all.
        ("is_date_before", "2026-01-01", "soon", False),
        ("is_date_before", "2026-01-01", True, False),
        ("is_date_before", "2026-01-01", None, False),
        ("is_date_before", "2026-01-01", ["2025-06-15"], False),
        # A form or a range that only the flags service resolves.
        ("is_date_before", "2026-01-01", "15/06/2025", "missing_context"),
        ("is_date_before", "2026-01-01", "2025-06-15T12:00:00+0200", "missing_context"),
        ("is_date_before", "2026-01-01", "2025-06-15t12:00:00z", "missing_context"),
        ("is_date_before", "2026-01-01", "2025-06-31", "missing_context"),
        ("is_date_before", "2026-01-01", "2025-06-15T23:59:60Z", "missing_context"),
        ("is_date_before", "2026-01-01", "99999d", "missing_context"),
        ("is_date_before", "2026-01-01", 1e12, "missing_context"),
        ("is_date_before", "2026-01-01", 0.9999999999, "missing_context"),
        ("is_date_after", "-2025y", "2025-06-15", "missing_context"),
    ],
)
def test_a_date_operator_compares_instants_like_the_flags_service(
    operator, filter_value, property_value, expected
):
    assert _match(operator, filter_value, property_value) == expected


@pytest.mark.parametrize(
    "filter_value, property_value, expected",
    [
        ("2025-06-15T12:00:00Z", "2025-06-15T14:00:00+02:00", True),
        ("2025-06-15T12:00:00Z", 1749988800, True),
        ("2025-06-15", "2025-06-15T00:00:00Z", "missing_context"),
        ("2025-06-15T12:00:00Z", "2025-06-15 12:00:00", "missing_context"),
        ("-1d", "2025-06-14T12:00:00Z", "missing_context"),
    ],
)
def test_a_date_that_needs_the_project_timezone_is_inconclusive_without_it(
    filter_value, property_value, expected
):
    result = _match("is_date_exact", filter_value, property_value, timezone=None)

    assert result == expected


def test_a_relative_date_counts_back_from_the_current_time_by_default():
    config = v2._parse_config(
        _filters(_targeted(_person("seen", "is_date_after", "-1h")))
    )
    # UTC has no skipped hour, so the hour before now always exists.
    utc = datetime.timezone.utc
    now = int(datetime.datetime.now(utc).timestamp())

    def evaluate(seen: int) -> v2._Outcome:
        context = v2._Context("visitor-42", {"seen": seen}, True, True, utc)
        return v2._evaluate(config, context)

    assert evaluate(now) == v2._Evaluated(
        True, "targeting_match", RULE_ID, "targeted_release", 0
    )
    assert evaluate(now - 7200) == v2._Evaluated(False, "no_rule_match")


def _hashes_drawn(
    filters: Dict[str, Any], identifier: str
) -> List[Tuple[str, str, str]]:
    """The hashes an evaluation draws, when every hash is 0.25."""
    drawn: List[Tuple[str, str, str]] = []

    def draw(use: str, seed: str, subject: str) -> float:
        drawn.append((use, seed, subject))
        return 0.25

    context = v2._Context(identifier, {}, True, True)
    v2._evaluate_with_hash(v2._parse_config(filters), context, draw)
    return drawn


def test_an_experiment_draws_its_holdout_rollout_and_variant_hashes_in_order():
    holdout = {"seed": "layout-holdout", "exclusion_percentage": 10}
    filters = _filters(_experiment(rollout_percentage=50, holdout=holdout))

    drawn = _hashes_drawn(filters, "v" * 250)

    assert drawn == [
        ("holdout", "layout-holdout", "v" * 200),
        ("rollout", "layout-test", "v" * 200),
        ("variant", "layout-test", "v" * 200),
    ]


def test_a_full_percentage_and_an_empty_identifier_are_decided_without_a_hash():
    holdout = {"seed": "layout-holdout", "exclusion_percentage": 100}

    assert _hashes_drawn(_filters(_experiment(holdout=holdout)), "visitor-42") == []
    assert _hashes_drawn(_filters(_experiment(rollout_percentage=50)), "") == []
    assert _hashes_drawn(_filters(_experiment()), "visitor-42") == [
        ("variant", "layout-test", "visitor-42")
    ]
