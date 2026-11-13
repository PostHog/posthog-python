"""Local evaluation of feature flags whose ``filters.version`` is 2.

Nothing in the SDK calls this module yet, and no name in it is public.

A config version 2 flag holds an ordered list of rules. The first rule whose targeting
matches decides the result, unless its rollout misses and the rule says to continue.
The flags service is the reference for every result this module returns.

:func:`_parse_config` reads a ``filters`` document. The parser is closed: an unknown
config version, rule type, property type, operator or field rejects the whole document,
so a document is never partly evaluated. :func:`_evaluate` returns exactly one of:

- :class:`_Evaluated`: the value, its reason code and the rule that produced it.
- :class:`_Inconclusive`: a rule's targeting needs something the evaluator does not
  have, such as a person property the caller did not pass. Evaluation stops there. It
  does not skip the rule to try a later one, because an earlier rule that matches is
  terminal.
- :class:`_Unsupported`: the parser rejected the document.
- :class:`_EvaluationError`: a property value or a regular expression is invalid.

None of them is a guess. A caller that cannot use the result must fall back to the
flags service, not to ``False`` or to the flag default.

The ``exact`` and text operators and regular expressions use the helpers of the version
1 evaluator, which already follow the flags service. Numbers, semantic versions and
dates are matched here, because version 1 reads them differently. Where Python cannot
reproduce the flags service, such as a date in a form only the service reads, the
result is inconclusive.
"""

from __future__ import annotations

import datetime
import hashlib
import math
import operator
import re
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    ClassVar,
    FrozenSet,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Tuple,
    Union,
)

from .feature_flags import (
    InconclusiveMatchError,
    _ascii_lower,
    _compile_regex,
    _match_exact_property,
    _match_regex,
    _match_text_property,
    _subtract_months,
    _subtract_years,
    _value_to_string,
)
from .types import UnresolvedFlagReason

_CONFIG_VERSION = 2
_ASSIGNMENT_ALGORITHM = "sha1_60_v1"
_HASH_SCALE = float(0xFFFFFFFFFFFFFFF)
# An identifier is cut to this many Unicode scalar values before it is hashed.
_MAX_IDENTIFIER_LENGTH = 200

_MAX_RULES = 100
_MAX_PREDICATES = 100
_MIN_VARIANTS = 2
_MAX_VARIANTS = 20
_MAX_SEED_LENGTH = 400
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_MAX_OBJECT_DEPTH = 20
# The reference's JSON reader gives up on deeper nesting.
_MAX_JSON_DEPTH = 127
_MAX_U64 = 2**64 - 1

_CONFIG_FIELDS = frozenset(
    {
        "version",
        "return_type",
        "default_value",
        "rules",
        "aggregation_group_type_index",
    }
)
_RULE_FIELDS = frozenset({"id", "rule_type", "targeting", "description", "metadata"})
_ROLLOUT_FIELDS = frozenset(
    {
        "rollout_percentage",
        "on_rollout_miss",
        "assignment_algorithm",
        "seed",
        "assign_by",
    }
)
_FIELDS_BY_RULE_TYPE = {
    "targeted_release": _RULE_FIELDS | {"value"},
    "percentage_rollout": _RULE_FIELDS | _ROLLOUT_FIELDS | {"value"},
    "experiment": _RULE_FIELDS | _ROLLOUT_FIELDS | {"paused", "variants", "holdout"},
}
_VARIANT_FIELDS = frozenset({"key", "weight", "value"})
_HOLDOUT_FIELDS = frozenset({"seed", "exclusion_percentage"})
_TARGETING_FIELDS = frozenset({"properties"})
_PROPERTY_FIELDS = frozenset(
    {
        "key",
        "type",
        "value",
        "operator",
        "negation",
        "group_type_index",
        "cohort_name",
        "group_key_names",
        "label",
    }
)
# Property types the configuration format defines but this evaluator does not read.
_UNSUPPORTED_PROPERTY_TYPES = frozenset({"cohort", "group", "flag"})

_EXACT_OPERATORS = frozenset({"exact", "is_not"})
_PRESENCE_OPERATORS = frozenset({"is_set", "is_not_set"})
_TEXT_OPERATORS = frozenset(
    {
        "icontains",
        "not_icontains",
        "starts_with",
        "not_starts_with",
        "ends_with",
        "not_ends_with",
    }
)
_MULTI_TEXT_OPERATORS = frozenset({"icontains_multi", "not_icontains_multi"})
_REGEX_OPERATORS = frozenset({"regex", "not_regex"})
_NUMBER_COMPARISONS: Mapping[str, Callable[[Any, Any], bool]] = {
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}
_SEMVER_COMPARISONS: Mapping[str, Callable[[Any, Any], bool]] = {
    "semver_eq": operator.eq,
    "semver_neq": operator.ne,
    "semver_gt": operator.gt,
    "semver_gte": operator.ge,
    "semver_lt": operator.lt,
    "semver_lte": operator.le,
}
_SEMVER_RANGE_OPERATORS = frozenset({"semver_tilde", "semver_caret", "semver_wildcard"})
_DATE_COMPARISONS: Mapping[str, Callable[[Any, Any], bool]] = {
    "is_date_exact": operator.eq,
    "is_date_after": operator.gt,
    "is_date_before": operator.lt,
}
_OPERATORS = (
    _EXACT_OPERATORS
    | _PRESENCE_OPERATORS
    | _TEXT_OPERATORS
    | _MULTI_TEXT_OPERATORS
    | _REGEX_OPERATORS
    | _SEMVER_RANGE_OPERATORS
    | frozenset(_NUMBER_COMPARISONS)
    | frozenset(_SEMVER_COMPARISONS)
    | frozenset(_DATE_COMPARISONS)
)
# The operators that match a person who does not have the property.
_OPERATORS_MATCHING_ABSENCE = frozenset(
    {
        "is_not",
        "not_icontains",
        "not_starts_with",
        "not_ends_with",
        "not_icontains_multi",
        "not_regex",
    }
)


# --- Results -------------------------------------------------------------------------


@dataclass(frozen=True)
class _Evaluated:
    """The flag has a result.

    ``value`` is the typed flag value: a boolean, string, number or object, or ``None``
    when the flag has no value and the caller's default applies. An object value is the
    configuration's own object, so it must not be changed. The rule fields name the rule
    that ended the evaluation, and are ``None`` when no rule did.
    """

    value: Any
    reason: str
    rule_id: Optional[str] = None
    rule_type: Optional[str] = None
    rule_index: Optional[int] = None
    variant_key: Optional[str] = None


@dataclass(frozen=True)
class _Inconclusive:
    """A rule that evaluation reached could not be decided."""

    reason: UnresolvedFlagReason
    detail: str


@dataclass(frozen=True)
class _Unsupported:
    """The parser rejected the document.

    ``kind`` is ``"malformed"`` for a document that breaks the configuration format,
    and ``"unsupported"`` for a format this evaluator does not read. ``field`` names
    what was rejected.
    """

    kind: str
    field: str


@dataclass(frozen=True)
class _EvaluationError:
    """Evaluation reached an invalid value.

    ``kind`` is ``"invalid_property"`` or ``"invalid_regex"``.
    """

    kind: str
    detail: str


_Outcome = Union[_Evaluated, _Inconclusive, _Unsupported, _EvaluationError]


class _Halt(Exception):
    """Ends an evaluation with an outcome that is not a value."""

    def __init__(self, outcome: Union[_Inconclusive, _EvaluationError]) -> None:
        super().__init__(outcome.detail)
        self.outcome = outcome


def _inconclusive(reason: UnresolvedFlagReason, detail: str) -> _Halt:
    return _Halt(_Inconclusive(reason, detail))


def _unusable_context(detail: str) -> _Halt:
    return _inconclusive(UnresolvedFlagReason.MISSING_CONTEXT, detail)


def _invalid_property(detail: str) -> _Halt:
    return _Halt(_EvaluationError("invalid_property", detail))


# --- Configuration -------------------------------------------------------------------


@dataclass(frozen=True)
class _Predicate:
    """One property filter of a rule.

    ``value`` is the filter value as stored. ``operand`` is what a number, semver or
    date operator compares with, read from ``value`` once by the parser.
    """

    key: str
    operator: str
    value: Any
    negation: bool
    operand: Any = None


@dataclass(frozen=True)
class _Rollout:
    percentage: float
    return_default_on_miss: bool
    seed: str


@dataclass(frozen=True)
class _Variant:
    key: str
    weight: float
    value: Any


@dataclass(frozen=True)
class _Holdout:
    seed: str
    exclusion_percentage: float


@dataclass(frozen=True)
class _TargetedRelease:
    rule_type: ClassVar[str] = "targeted_release"

    id: str
    targeting: Tuple[_Predicate, ...]
    value: Any


@dataclass(frozen=True)
class _PercentageRollout:
    rule_type: ClassVar[str] = "percentage_rollout"

    id: str
    targeting: Tuple[_Predicate, ...]
    value: Any
    rollout: _Rollout


@dataclass(frozen=True)
class _Experiment:
    rule_type: ClassVar[str] = "experiment"

    id: str
    targeting: Tuple[_Predicate, ...]
    paused: bool
    rollout: _Rollout
    variants: Tuple[_Variant, ...]
    holdout: Optional[_Holdout]


_Rule = Union[_TargetedRelease, _PercentageRollout, _Experiment]


@dataclass(frozen=True)
class _Config:
    """A parsed ``filters`` document. ``default_value`` is ``None`` for a null default."""

    default_value: Any
    rules: Tuple[_Rule, ...]


@dataclass(frozen=True)
class _Context:
    """What a flag is evaluated for.

    Attributes:
        identifier: The person's resolved targeting key, as a string.
        properties: The person properties, or ``None`` when there are none to read.
        properties_complete: Whether ``properties`` holds every property of the person.
            A complete map shows that a property is absent. A partial map does not, so
            a filter on a key it lacks is inconclusive.
        explicit_exact_matching: The project's setting for the ``exact`` and ``is_not``
            operators. When false, a filter value that looks like a boolean matches by
            truthiness.
        timezone: The project's timezone, as a ``zoneinfo.ZoneInfo`` or another
            ``tzinfo`` that honours ``fold``. Dates without a UTC offset and relative
            dates are read in it. ``None`` makes a filter that needs it inconclusive.
        now: The time relative dates count back from, as an aware datetime. ``None``
            means the current time.
    """

    identifier: str
    properties: Optional[Mapping[str, Any]]
    properties_complete: bool
    explicit_exact_matching: bool
    timezone: Optional[datetime.tzinfo] = None
    now: Optional[datetime.datetime] = None


# --- Parser --------------------------------------------------------------------------


class _Rejected(Exception):
    def __init__(self, kind: str, field: str) -> None:
        super().__init__(field)
        self.kind = kind
        self.field = field


def _malformed(field: str) -> _Rejected:
    return _Rejected("malformed", field)


def _unsupported(field: str) -> _Rejected:
    return _Rejected("unsupported", field)


def _parse_config(filters: Any) -> Union[_Config, _Unsupported]:
    """Read the ``filters`` document of a flag as config version 2.

    The document is the decoded JSON. Three things the reference rejects in the JSON
    text cannot be seen here: a repeated object key, a nonzero number that rounds to
    zero, and a percentage written with more than two decimals that rounds to a
    two-decimal value.
    """
    try:
        return _read_config(filters)
    except _Rejected as rejected:
        return _Unsupported(rejected.kind, rejected.field)


def _read_config(filters: Any) -> _Config:
    document = _object(filters, "filters")
    version = document.get("version")
    if not _is_number(version) or version != _CONFIG_VERSION:
        raise _unsupported("version")
    _check_json(document)
    _check_closed(document, _CONFIG_FIELDS, "filters")
    return_type = _string(document, "return_type")
    if return_type not in _VALUE_CHECKS:
        raise _malformed("return_type")
    if "aggregation_group_type_index" in document:
        group = document["aggregation_group_type_index"]
        if not _is_number(group) or not float(group).is_integer():
            raise _malformed("aggregation_group_type_index")
        raise _unsupported("aggregation_group_type_index")
    default_value = _required(document, "default_value")
    if default_value is not None and not _VALUE_CHECKS[return_type](default_value):
        raise _malformed("default_value")
    rules = _array(document, "rules", _MAX_RULES)
    parsed = tuple(_read_rule(rule, return_type) for rule in rules)
    if len({rule.id for rule in parsed}) != len(parsed):
        raise _malformed("rule.id")
    return _Config(default_value, parsed)


def _check_json(document: Any) -> None:
    """Reject values that the reference's JSON reader does not produce."""
    pending = [(document, 1)]
    while pending:
        value, depth = pending.pop()
        if isinstance(value, (dict, list)):
            if depth > _MAX_JSON_DEPTH:
                raise _malformed("filters")
            if isinstance(value, dict):
                if not all(isinstance(key, str) and _is_text(key) for key in value):
                    raise _malformed("filters")
                value = value.values()
            pending.extend((item, depth + 1) for item in value)
        elif not _is_json_scalar(value):
            raise _malformed("filters")


def _is_json_scalar(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, str):
        return _is_text(value)
    if not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _is_text(value: str) -> bool:
    """Whether a string holds only Unicode scalar values, so no lone surrogate."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _object(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise _malformed(field)
    return value


def _required(mapping: Mapping[str, Any], field: str) -> Any:
    if field not in mapping:
        raise _malformed(field)
    return mapping[field]


def _string(mapping: Mapping[str, Any], field: str) -> str:
    value = _required(mapping, field)
    if not isinstance(value, str):
        raise _malformed(field)
    return value


def _array(mapping: Mapping[str, Any], field: str, limit: int) -> List[Any]:
    value = _required(mapping, field)
    if not isinstance(value, list) or len(value) > limit:
        raise _malformed(field)
    return value


def _check_closed(
    mapping: Mapping[str, Any], fields: FrozenSet[str], field: str
) -> None:
    if not fields.issuperset(mapping):
        raise _malformed(field)


def _is_safe_number(value: Any) -> bool:
    return _is_number(value) and -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER


def _is_object_value(value: Any) -> bool:
    return isinstance(value, dict) and _is_nested_value(value, 1)


def _is_nested_value(value: Any, depth: int) -> bool:
    """Depth counts objects and arrays, with the returned object at level 1."""
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return depth <= _MAX_OBJECT_DEPTH and all(
            _is_nested_value(item, depth + 1) for item in value
        )
    return not _is_number(value) or _is_safe_number(value)


# Whether a value is a non-null value of the flag's return type.
_VALUE_CHECKS: Mapping[str, Callable[[Any], bool]] = {
    "boolean": lambda value: isinstance(value, bool),
    "string": lambda value: isinstance(value, str) and value != "",
    "number": _is_safe_number,
    "object": _is_object_value,
}

_RULE_ID_PATTERN = re.compile(
    "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_VARIANT_KEY_PATTERN = re.compile("[A-Za-z0-9_-]+")


def _read_rule(value: Any, return_type: str) -> _Rule:
    rule = _object(value, "rule")
    rule_type = _string(rule, "rule_type")
    if rule_type not in _FIELDS_BY_RULE_TYPE:
        raise _malformed("rule_type")
    _check_closed(rule, _FIELDS_BY_RULE_TYPE[rule_type], "rule")
    rule_id = _string(rule, "id")
    if not _RULE_ID_PATTERN.fullmatch(rule_id):
        raise _malformed("rule.id")
    rule_id = rule_id.lower()
    if "description" in rule and not isinstance(rule["description"], str):
        raise _malformed("description")
    if "metadata" in rule and not isinstance(rule["metadata"], dict):
        raise _malformed("metadata")
    targeting = _read_targeting(_required(rule, "targeting"))
    if rule_type == "targeted_release":
        return _TargetedRelease(rule_id, targeting, _typed_value(rule, return_type))
    if rule_type == "percentage_rollout":
        rollout = _read_rollout(rule)
        return _PercentageRollout(
            rule_id, targeting, _typed_value(rule, return_type), rollout
        )
    paused = _required(rule, "paused")
    if not isinstance(paused, bool):
        raise _malformed("paused")
    return _Experiment(
        rule_id,
        targeting,
        paused,
        _read_rollout(rule),
        _read_variants(rule, return_type),
        _read_holdout(rule["holdout"]) if "holdout" in rule else None,
    )


def _typed_value(mapping: Mapping[str, Any], return_type: str) -> Any:
    value = _required(mapping, "value")
    if not _VALUE_CHECKS[return_type](value):
        raise _malformed("value")
    return value


def _read_rollout(rule: Mapping[str, Any]) -> _Rollout:
    if _string(rule, "assignment_algorithm") != _ASSIGNMENT_ALGORITHM:
        raise _malformed("assignment_algorithm")
    if "assign_by" in rule and rule["assign_by"] != "person":
        raise _malformed("assign_by")
    on_rollout_miss = _string(rule, "on_rollout_miss")
    if on_rollout_miss not in ("continue", "return_default"):
        raise _malformed("on_rollout_miss")
    return _Rollout(
        _percentage(rule, "rollout_percentage"),
        on_rollout_miss == "return_default",
        _seed(rule),
    )


def _percentage(mapping: Mapping[str, Any], field: str) -> float:
    value = _required(mapping, field)
    # A percentage has at most two decimals.
    if not _is_number(value) or not 0 <= value <= 100 or round(value, 2) != value:
        raise _malformed(field)
    return value


def _seed(mapping: Mapping[str, Any]) -> str:
    seed = _string(mapping, "seed")
    if not 1 <= len(seed) <= _MAX_SEED_LENGTH:
        raise _malformed("seed")
    return seed


def _read_variants(rule: Mapping[str, Any], return_type: str) -> Tuple[_Variant, ...]:
    items = _array(rule, "variants", _MAX_VARIANTS)
    if len(items) < _MIN_VARIANTS:
        raise _malformed("variants")
    variants = tuple(_read_variant(item, return_type) for item in items)
    if len({variant.key for variant in variants}) != len(variants):
        raise _malformed("variant.key")
    # Weights have two decimals, so whole hundredths add up exactly.
    if sum(math.floor(variant.weight * 100 + 0.5) for variant in variants) != 10_000:
        raise _malformed("variants")
    return variants


def _read_variant(value: Any, return_type: str) -> _Variant:
    variant = _object(value, "variant")
    _check_closed(variant, _VARIANT_FIELDS, "variant")
    key = _string(variant, "key")
    if not _VARIANT_KEY_PATTERN.fullmatch(key):
        raise _malformed("variant.key")
    return _Variant(
        key, _percentage(variant, "weight"), _typed_value(variant, return_type)
    )


def _read_holdout(value: Any) -> _Holdout:
    holdout = _object(value, "holdout")
    _check_closed(holdout, _HOLDOUT_FIELDS, "holdout")
    return _Holdout(_seed(holdout), _percentage(holdout, "exclusion_percentage"))


def _read_targeting(value: Any) -> Tuple[_Predicate, ...]:
    targeting = _object(value, "targeting")
    _check_closed(targeting, _TARGETING_FIELDS, "targeting")
    properties = _array(targeting, "properties", _MAX_PREDICATES)
    return tuple(_read_predicate(item) for item in properties)


def _read_predicate(value: Any) -> _Predicate:
    prop = _object(value, "property")
    _check_closed(prop, _PROPERTY_FIELDS, "property")
    property_type = _string(prop, "type")
    if property_type in _UNSUPPORTED_PROPERTY_TYPES:
        raise _unsupported("property.type")
    if property_type != "person":
        raise _malformed("property.type")
    key = _string(prop, "key")
    if key == "":
        raise _malformed("property.key")
    name = prop.get("operator")
    if name is None:
        name = "exact"
    elif not isinstance(name, str) or name not in _OPERATORS:
        raise _malformed("property.operator")
    negation = prop.get("negation")
    if negation is not None and not isinstance(negation, bool):
        raise _malformed("property.negation")
    _check_unread_fields(prop)
    filter_value = prop.get("value")
    return _Predicate(
        key, name, filter_value, bool(negation), _read_operand(name, filter_value)
    )


def _check_unread_fields(prop: Mapping[str, Any]) -> None:
    """Check the fields a person filter may carry but evaluation does not read."""
    if prop.get("group_type_index") is not None:
        raise _malformed("property.group_type_index")
    for field in ("cohort_name", "label"):
        if not isinstance(prop.get(field), (str, type(None))):
            raise _malformed("property.presentation")
    names = prop.get("group_key_names")
    if names is not None and not (
        isinstance(names, dict)
        and all(isinstance(name, str) for name in names.values())
    ):
        raise _malformed("property.group_key_names")


def _read_operand(name: str, value: Any) -> Any:
    """Check a filter value against its operator, and read what the operator compares with."""
    if name in _NUMBER_COMPARISONS:
        if isinstance(value, str) or _is_number(value):
            # A string that is not a number is an evaluation error, not a parse error.
            return _to_number(value)
    elif name in _SEMVER_COMPARISONS or name in _SEMVER_RANGE_OPERATORS:
        if isinstance(value, str):
            return _read_semver_filter(name, value)
    elif name in _DATE_COMPARISONS:
        if isinstance(value, str):
            return _read_date_filter(value)
    elif name in _MULTI_TEXT_OPERATORS:
        if isinstance(value, list):
            return None
    elif name in _REGEX_OPERATORS or name in _TEXT_OPERATORS:
        if isinstance(value, str):
            return None
    else:
        return None
    raise _malformed("property.value")


# --- Evaluation ----------------------------------------------------------------------


def _evaluate(
    config: Union[_Config, _Unsupported], context: _Context, *, active: bool = True
) -> _Outcome:
    """Evaluate a parsed flag for one person.

    ``active`` is the flag's own ``active`` field. An inactive flag has no value, with
    the reason ``flag_disabled``.
    """
    return _evaluate_with_hash(config, context, _draw, active=active)


def _evaluate_with_hash(
    config: Union[_Config, _Unsupported],
    context: _Context,
    draw: Callable[[str, str, str], float],
    *,
    active: bool = True,
) -> _Outcome:
    """Test seam: ``draw(use, seed, subject)`` replaces every hash an evaluation draws."""
    if not active:
        return _Evaluated(None, "flag_disabled")
    if isinstance(config, _Unsupported):
        return config
    draws = _Draws(context.identifier[:_MAX_IDENTIFIER_LENGTH], draw)
    try:
        for index, rule in enumerate(config.rules):
            if not all(_matches(predicate, context) for predicate in rule.targeting):
                continue
            result = _apply_rule(rule, index, config.default_value, draws)
            if result is not None:
                return result
            # The rollout missed and the rule says to continue.
    except _Halt as halt:
        return halt.outcome
    except InconclusiveMatchError as error:
        return _Inconclusive(error.reason, str(error))
    except UnicodeEncodeError:
        return _Inconclusive(
            UnresolvedFlagReason.MISSING_CONTEXT,
            "The identifier is not valid Unicode, so it cannot be hashed",
        )
    return _Evaluated(config.default_value, "no_rule_match")


def _apply_rule(
    rule: _Rule, index: int, default_value: Any, draws: _Draws
) -> Optional[_Evaluated]:
    """The result of a rule whose targeting matched, or ``None`` to try the next rule."""

    def result(
        reason: str, value: Any, variant_key: Optional[str] = None
    ) -> _Evaluated:
        return _Evaluated(value, reason, rule.id, rule.rule_type, index, variant_key)

    if isinstance(rule, _TargetedRelease):
        return result("targeting_match", rule.value)
    if isinstance(rule, _Experiment):
        # A paused rule and a holdout are terminal whatever the rule says about a miss.
        if rule.paused:
            return result("experiment_paused", default_value)
        holdout = rule.holdout
        if holdout is not None and draws.includes(
            "holdout", holdout.seed, holdout.exclusion_percentage
        ):
            return result("holdout", default_value)
    rollout = rule.rollout
    if draws.includes("rollout", rollout.seed, rollout.percentage):
        if isinstance(rule, _PercentageRollout):
            return result("targeting_match", rule.value)
        variant = _select_variant(draws.draw("variant", rollout.seed), rule.variants)
        return result("experiment_split", variant.value, variant.key)
    if rollout.return_default_on_miss:
        return result("rollout_miss", default_value)
    return None


# --- Hashing -------------------------------------------------------------------------


def _hash01(prefix: str, identifier: str, salt: str) -> float:
    """The ``sha1_60_v1`` hash: the first 60 bits of a SHA-1 digest, scaled to 0..1."""
    digest = hashlib.sha1((prefix + identifier + salt).encode("utf-8")).hexdigest()
    # Both operands are binary64 before the one division.
    return float(int(digest[:15], 16)) / _HASH_SCALE


def _draw(use: str, seed: str, subject: str) -> float:
    if use == "holdout":
        # No ".": the seed is the whole prefix.
        return _hash01(seed, subject, "")
    return _hash01(seed + ".", subject, "variant" if use == "variant" else "")


def _in_rollout(percentage: float, draw: Callable[[], float]) -> bool:
    """100 percent includes without hashing. Below it, the comparison is inclusive."""
    return percentage == 100 or draw() <= percentage / 100


def _select_variant(hash01: float, variants: Tuple[_Variant, ...]) -> _Variant:
    """Boundaries add left to right. Past the last one, the last variant wins."""
    boundary = 0.0
    for variant in variants:
        boundary += variant.weight / 100
        if hash01 < boundary:
            return variant
    return variants[-1]


class _Draws:
    """The hashes one evaluation draws for its subject."""

    def __init__(self, subject: str, draw: Callable[[str, str, str], float]) -> None:
        self._subject = subject
        self._draw = draw

    def draw(self, use: str, seed: str) -> float:
        return self._draw(use, seed, self._subject)

    def includes(self, use: str, seed: str, percentage: float) -> bool:
        # An empty subject never enters a rollout or a holdout, so it is never hashed.
        return self._subject != "" and _in_rollout(
            percentage, lambda: self.draw(use, seed)
        )


# --- Property matching ---------------------------------------------------------------


def _matches(predicate: _Predicate, context: _Context) -> bool:
    """Whether a predicate holds for the person, after its negation."""
    properties = context.properties
    if properties is None:
        raise _unusable_context("No person properties were passed")
    present = predicate.key in properties
    if not present and not context.properties_complete:
        raise _unusable_context(f"The person property '{predicate.key}' was not passed")
    # An invalid pattern is an error even when the person lacks the property, and
    # negation does not turn it into a match.
    if (
        predicate.operator in _REGEX_OPERATORS
        and _compile_regex(predicate.value) is None
    ):
        raise _Halt(
            _EvaluationError(
                "invalid_regex", f"The pattern for '{predicate.key}' is not valid"
            )
        )
    matched = _match_operator(
        predicate, present, properties.get(predicate.key), context
    )
    return matched != predicate.negation


def _match_operator(
    predicate: _Predicate, present: bool, value: Any, context: _Context
) -> bool:
    name = predicate.operator
    if name in _PRESENCE_OPERATORS:
        return present if name == "is_set" else not present
    if predicate.value is None:
        return False
    if not present:
        return name in _OPERATORS_MATCHING_ABSENCE
    if name in _EXACT_OPERATORS:
        matching_version = 2 if context.explicit_exact_matching else 1
        matched = _match_exact_property(predicate.value, value, matching_version)
        return matched if name == "exact" else not matched
    if name in _TEXT_OPERATORS:
        return _match_text_property(name, predicate.value, value)
    if name in _MULTI_TEXT_OPERATORS:
        return _match_any_substring(predicate, value)
    if name in _REGEX_OPERATORS:
        text = _value_to_string(value, UnresolvedFlagReason.MISSING_CONTEXT)
        found = bool(_match_regex(predicate.value, text))
        return found if name == "regex" else not found
    if name in _NUMBER_COMPARISONS:
        return _match_number(predicate, value)
    if name in _DATE_COMPARISONS:
        return _match_date(predicate, value, context)
    return _match_semver(predicate, value)


def _match_any_substring(predicate: _Predicate, value: Any) -> bool:
    text = _ascii_lower(value, UnresolvedFlagReason.MISSING_CONTEXT)
    found = any(
        _ascii_lower(item, UnresolvedFlagReason.UNSUPPORTED_DEFINITION) in text
        for item in predicate.value
    )
    return found if predicate.operator == "icontains_multi" else not found


def _match_number(predicate: _Predicate, value: Any) -> bool:
    number = _to_number(value)
    if number is None:
        raise _invalid_property(
            f"The person property '{predicate.key}' is not a number"
        )
    if predicate.operand is None:
        raise _invalid_property(
            f"The filter value for '{predicate.key}' is not a number"
        )
    return _NUMBER_COMPARISONS[predicate.operator](number, predicate.operand)


# The decimal numbers the reference reads from a string. Python's ``float`` also takes
# surrounding whitespace and underscores.
_NUMBER_PATTERN = re.compile(
    r"[+-]?(?:inf|infinity|nan|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:e[+-]?[0-9]+)?)",
    re.IGNORECASE,
)


def _to_number(value: Any) -> Optional[float]:
    """Read a JSON number or a numeric string as binary64, or ``None`` for anything else."""
    if isinstance(value, str):
        return float(value) if _NUMBER_PATTERN.fullmatch(value) else None
    if not _is_number(value):
        if value is None or isinstance(value, (bool, list, dict)):
            return None
        raise _unusable_context(
            f"A property value of type {type(value).__name__} is not a JSON value"
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise _unusable_context("A property value is not a finite number")
    try:
        return float(value)
    except OverflowError:
        raise _unusable_context("A property value is too large to compare") from None


# --- Semantic versions ---------------------------------------------------------------
#
# The reference compares versions with full semantic version ordering, so a pre-release
# sorts below its release, and a range matches a pre-release only when the range names
# one for the same version.

# What the reference trims from a version string: the Unicode White_Space characters.
# Python's str.strip() also trims U+001C to U+001F.
_WHITESPACE = "".join(
    map(
        chr,
        [*range(0x09, 0x0E), 0x20, 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B)]
        + [0x2028, 0x2029, 0x202F, 0x205F, 0x3000],
    )
)
_INTEGER = "(?:0|[1-9][0-9]*)"
_PRE_RELEASE_PART = f"(?:{_INTEGER}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_PRE_RELEASE = rf"{_PRE_RELEASE_PART}(?:\.{_PRE_RELEASE_PART})*"
_BUILD = r"[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*"
_VERSION_PATTERN = re.compile(
    rf"({_INTEGER})\.({_INTEGER})\.({_INTEGER})(?:-({_PRE_RELEASE}))?(?:\+({_BUILD}))?"
)
# A version requirement: a version that may end early, or in wildcard components.
_REQUIREMENT_PATTERN = re.compile(
    rf"(?P<major>{_INTEGER})"
    rf"(?:(?P<any_minor>\.[xX*](?:\.[xX*])?)"
    rf"|\.(?P<minor>{_INTEGER})"
    rf"(?:(?P<any_patch>\.[xX*])"
    rf"|\.(?P<patch>{_INTEGER})(?:-(?P<pre>{_PRE_RELEASE}))?(?:\+{_BUILD})?)?)?"
)
_UNSIGNED_INTEGER_PATTERN = re.compile(r"\+?([0-9]+)")


class _Version(NamedTuple):
    major: int
    minor: int
    patch: int
    pre: str
    build: str


class _Requirement(NamedTuple):
    """A version range. ``minor`` and ``patch`` are ``None`` where any number matches."""

    kind: str  # "tilde", "caret" or "wildcard"
    major: int
    minor: Optional[int]
    patch: Optional[int]
    pre: str


def _is_u64(digits: str) -> bool:
    digits = digits.lstrip("0")
    return len(digits) <= 20 and int(digits or "0") <= _MAX_U64


def _parse_version(text: str) -> Optional[_Version]:
    """Read a version the way the reference does, or ``None`` when it is not one.

    One leading ``v`` is dropped, a missing minor or patch number is zero, and leading
    zeros of the three numbers are ignored.
    """
    match = _VERSION_PATTERN.fullmatch(_canonical_version(_trim_version(text)))
    if match is None or not all(_is_u64(number) for number in match.group(1, 2, 3)):
        return None
    major, minor, patch = (int(number) for number in match.group(1, 2, 3))
    return _Version(major, minor, patch, match.group(4) or "", match.group(5) or "")


def _trim_version(text: str) -> str:
    return (text[1:] if text.startswith("v") else text).strip(_WHITESPACE)


def _canonical_version(version: str) -> str:
    suffix = re.search("[-+]", version)
    core = version[: suffix.start()] if suffix else version
    parts = [_strip_leading_zeros(part) for part in core.split(".")]
    parts += ["0"] * (3 - len(parts))
    return ".".join(parts) + version[len(core) :]


def _strip_leading_zeros(part: str) -> str:
    if not re.fullmatch("[0-9]+", part):
        return part
    return part.lstrip("0") or "0"


def _pre_release_key(pre: str) -> Tuple[Any, ...]:
    """Order pre-releases: a release is above each of its pre-releases."""
    if pre == "":
        return (1,)
    # A numeric part is below a text part, and numeric parts compare as numbers.
    return (
        0,
        [
            (0, len(part), part) if part.isdigit() else (1, part)
            for part in pre.split(".")
        ],
    )


def _build_key(build: str) -> List[Tuple[Any, ...]]:
    parts: List[Tuple[Any, ...]] = []
    for part in build.split("."):
        if part == "" or part.isdigit():
            digits = part.lstrip("0")
            parts.append((0, len(digits), digits, len(part)))
        else:
            parts.append((1, part))
    return parts


def _version_key(version: _Version) -> Tuple[Any, ...]:
    return (
        version.major,
        version.minor,
        version.patch,
        _pre_release_key(version.pre),
        _build_key(version.build),
    )


def _read_semver_filter(name: str, text: str) -> Any:
    """Read a semver filter value, or reject the document.

    The result is the sort key of the version to compare with, or the range to match.
    It is ``None`` for a wildcard that the configuration format admits but that is not
    a range, such as ``1.2.3.*``. Evaluation reports that as an invalid property.

    The format requires plain numbers before any pre-release suffix, so it does not
    admit everything :func:`_parse_version` reads, such as a leading ``v``.
    """
    admitted = text.rstrip(".*") if name == "semver_wildcard" else text
    parsed = _parse_version(admitted)
    numbers = admitted.split("-")[0].split(".")[:3]
    if parsed is None or not all(_is_plain_number(number) for number in numbers):
        raise _malformed("property.value")
    if name in _SEMVER_COMPARISONS:
        return _version_key(parsed)
    version = _trim_version(text)
    if name == "semver_wildcard":
        parts = (_strip_leading_zeros(part) for part in version.split("."))
        # The reference writes "*" as "x", which is a letter inside a pre-release.
        return _parse_requirement(".".join(parts).replace("*", "x"), None)
    return _parse_requirement(_canonical_version(version), name[len("semver_") :])


def _is_plain_number(text: str) -> bool:
    number = _UNSIGNED_INTEGER_PATTERN.fullmatch(text.strip(_WHITESPACE))
    return number is not None and _is_u64(number.group(1))


def _parse_requirement(text: str, kind: Optional[str]) -> Optional[_Requirement]:
    match = _REQUIREMENT_PATTERN.fullmatch(text)
    if match is None:
        return None
    major, minor, patch = match.group("major", "minor", "patch")
    if not all(_is_u64(number) for number in (major, minor, patch) if number):
        return None
    if kind is None:
        # Without a wildcard the reference reads a bare version as a caret range.
        wildcard = match.group("any_minor") or match.group("any_patch")
        kind = "wildcard" if wildcard else "caret"
    return _Requirement(
        kind,
        int(major),
        int(minor) if minor else None,
        int(patch) if patch else None,
        match.group("pre") or "",
    )


def _match_semver(predicate: _Predicate, value: Any) -> bool:
    text = _value_to_string(value, UnresolvedFlagReason.MISSING_CONTEXT)
    version = _parse_version(text)
    if version is None:
        return False
    if predicate.operator in _SEMVER_COMPARISONS:
        compare = _SEMVER_COMPARISONS[predicate.operator]
        return compare(_version_key(version), predicate.operand)
    if predicate.operand is None:
        raise _invalid_property(
            f"The filter value for '{predicate.key}' is not a version range"
        )
    return _satisfies(version, predicate.operand)


def _satisfies(version: _Version, requirement: _Requirement) -> bool:
    if not _in_range(version, requirement):
        return False
    # A pre-release matches only a range that names a pre-release of the same version.
    return version.pre == "" or (
        requirement.pre != "" and version[:3] == requirement[1:4]
    )


def _in_range(version: _Version, requirement: _Requirement) -> bool:
    kind, major, minor, patch, pre = requirement
    if version.major != major:
        return False
    if kind == "wildcard":
        return (
            minor in (None, version.minor)
            and patch in (None, version.patch)
            and version.pre == pre
        )
    reached = _pre_release_key(version.pre) >= _pre_release_key(pre)
    if kind == "tilde":
        if minor is not None and version.minor != minor:
            return False
        if patch is not None and version.patch != patch:
            return version.patch > patch
        return reached
    # A caret range keeps the leftmost number that is not zero.
    if minor is None:
        return True
    if patch is None:
        return version.minor >= minor if major > 0 else version.minor == minor
    if major > 0:
        if version.minor != minor:
            return version.minor > minor
        if version.patch != patch:
            return version.patch > patch
    elif minor > 0:
        if version.minor != minor:
            return False
        if version.patch != patch:
            return version.patch > patch
    elif (version.minor, version.patch) != (minor, patch):
        return False
    return reached


# --- Dates ---------------------------------------------------------------------------
#
# An instant is (seconds since the Unix epoch, nanoseconds). The reference compares
# with nanosecond precision, which ``datetime`` does not have.

_Instant = Tuple[int, int]

_EPOCH_ORDINAL = datetime.date(1970, 1, 1).toordinal()
# Epoch seconds that fall in the years 1 to 9999.
_MIN_EPOCH_SECONDS = -62_135_596_800
_MAX_EPOCH_SECONDS = 253_402_300_799
_RELATIVE_DATE_PATTERN = re.compile("-?([0-9]+)([hdwmy])")
_DATE_PATTERN = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})"
    r"(?:(?P<separator>[T ])([0-9]{2}):([0-9]{2})"
    r"(?::(?P<second>[0-9]{2})(?:\.(?P<fraction>[0-9]{1,9}))?)?"
    r"(?P<offset>Z|[+-][0-9]{2}:[0-9]{2})?)?"
)


class _RelativeDate(NamedTuple):
    """A date such as ``-7d``: seven days before now. The sign changes nothing."""

    number: int
    interval: str


class _AbsoluteDate(NamedTuple):
    wall_clock: datetime.datetime  # whole seconds, no timezone
    nanoseconds: int
    offset: Optional[int]  # seconds east of UTC; None means the project's timezone


_Date = Union[_RelativeDate, _AbsoluteDate]


def _parse_date(text: str) -> Optional[_Date]:
    """Read a date in one of the forms this evaluator resolves exactly.

    These are a relative date, an RFC 3339 timestamp, a bare ``YYYY-MM-DD`` date, and
    a date and time without an offset. The reference reads more forms on a best-effort
    basis, so ``None`` does not mean the reference cannot read the text.
    """
    relative = _RELATIVE_DATE_PATTERN.fullmatch(text)
    if relative is not None:
        digits = relative.group(1).lstrip("0")
        if len(digits) > 4:
            # 10,000 and above is not a relative date.
            return None
        return _RelativeDate(int(digits or "0"), relative.group(2))
    match = _DATE_PATTERN.fullmatch(text)
    if match is None:
        return None
    offset = _parse_offset(match.group("offset"))
    if match.group("offset") is not None and (
        offset is None
        or match.group("separator") != "T"
        or match.group("second") is None
    ):
        # A timestamp with an offset is RFC 3339 or nothing.
        return None
    year, month, day, hour, minute = (
        int(part or 0) for part in match.group(1, 2, 3, 5, 6)
    )
    try:
        wall_clock = datetime.datetime(
            year, month, day, hour, minute, int(match.group("second") or 0)
        )
    except ValueError:
        return None
    fraction = match.group("fraction") or ""
    return _AbsoluteDate(wall_clock, int(fraction.ljust(9, "0")), offset)


def _parse_offset(text: Optional[str]) -> Optional[int]:
    if text is None:
        return None
    if text == "Z":
        return 0
    hours, minutes = int(text[1:3]), int(text[4:6])
    if hours > 23 or minutes > 59:
        return None
    seconds = hours * 3600 + minutes * 60
    return -seconds if text[0] == "-" else seconds


def _read_date_filter(text: str) -> _Date:
    """Read a date filter value, or reject the document.

    The configuration format does not admit a date and time joined by ``T`` without an
    offset, although the reference would evaluate one.
    """
    date = _parse_date(text)
    if date is None or (
        isinstance(date, _AbsoluteDate) and date.offset is None and "T" in text
    ):
        raise _unsupported("property.value")
    return date


def _match_date(predicate: _Predicate, value: Any, context: _Context) -> bool:
    subject = _property_instant(value, context)
    if subject is None:
        return False
    target = _resolve_date(predicate.operand, context)
    if target is None:
        return False
    return _DATE_COMPARISONS[predicate.operator](subject, target)


def _property_instant(value: Any, context: _Context) -> Optional[_Instant]:
    """The instant a person property holds, or ``None`` when it holds no date."""
    if value is None or isinstance(value, (bool, list, dict)):
        return None
    if _is_number(value):
        return _epoch_instant(value)
    if not isinstance(value, str):
        raise _unusable_context(
            f"A property value of type {type(value).__name__} is not a JSON value"
        )
    # A number, also as a string, is Unix epoch seconds.
    if _NUMBER_PATTERN.fullmatch(value):
        return _epoch_instant(float(value))
    date = _parse_date(value)
    if date is not None:
        return _resolve_date(date, context)
    if not re.search("[0-9]", value):
        return None
    raise _unusable_context(
        "The date property is in a form that needs the flags service to read"
    )


def _epoch_instant(value: Union[int, float]) -> _Instant:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _unusable_context("A date property is not a finite number")
        seconds = math.floor(value)
        # The reference drops the fraction of a negative number.
        nanoseconds = max(0, _round_half_up(math.fmod(value, 1.0) * 1e9))
    else:
        seconds, nanoseconds = value, 0
    if (
        not _MIN_EPOCH_SECONDS <= seconds <= _MAX_EPOCH_SECONDS
        or nanoseconds >= 1_000_000_000
    ):
        raise _unusable_context("A date property is outside the supported range")
    return (seconds, nanoseconds)


def _round_half_up(value: float) -> int:
    whole = math.floor(value)
    return whole + 1 if value - whole >= 0.5 else whole


def _resolve_date(date: _Date, context: _Context) -> Optional[_Instant]:
    """The instant of a date, or ``None`` when its wall clock does not exist.

    A wall clock that a spring-forward transition skips has no instant. One that a
    fall-back transition repeats means its earlier instant.
    """
    if isinstance(date, _RelativeDate):
        wall_clock = _relative_wall_clock(date, context)
        nanoseconds = wall_clock.microsecond * 1000
        return _local_instant(wall_clock.replace(microsecond=0), nanoseconds, context)
    if date.offset is None:
        return _local_instant(date.wall_clock, date.nanoseconds, context)
    return _instant(date.wall_clock, date.nanoseconds, date.offset)


def _timezone(context: _Context) -> datetime.tzinfo:
    if context.timezone is None:
        raise _unusable_context("The project's timezone is not known")
    return context.timezone


def _relative_wall_clock(date: _RelativeDate, context: _Context) -> datetime.datetime:
    """Count back from now on the wall clock of the project's timezone.

    A day is a calendar day there, not 24 hours. Months and years go back one at a
    time, and each step clamps the day to the end of its month.
    """
    now = context.now or datetime.datetime.now(datetime.timezone.utc)
    local = now.astimezone(_timezone(context)).replace(tzinfo=None)
    moved: Optional[datetime.datetime]
    try:
        if date.interval == "m":
            moved = _subtract_months(local, date.number)
        elif date.interval == "y":
            moved = _subtract_years(local, date.number)
        else:
            hours = date.number * {"h": 1, "d": 24, "w": 168}[date.interval]
            moved = local - datetime.timedelta(hours=hours)
    except OverflowError:
        moved = None
    if moved is None:
        raise _unusable_context("A relative date is outside the supported range")
    return moved


def _local_instant(
    wall_clock: datetime.datetime, nanoseconds: int, context: _Context
) -> Optional[_Instant]:
    timezone = _timezone(context)
    first = wall_clock.replace(tzinfo=timezone, fold=0).utcoffset()
    second = wall_clock.replace(tzinfo=timezone, fold=1).utcoffset()
    if first is None or second is None:
        raise _unusable_context("The project's timezone has no UTC offset")
    if first < second:
        # The offset grows across a spring-forward gap, so this wall clock is in one.
        return None
    return _instant(wall_clock, nanoseconds, int(first.total_seconds()))


def _instant(wall_clock: datetime.datetime, nanoseconds: int, offset: int) -> _Instant:
    days = wall_clock.toordinal() - _EPOCH_ORDINAL
    seconds = wall_clock.hour * 3600 + wall_clock.minute * 60 + wall_clock.second
    return (days * 86_400 + seconds - offset, nanoseconds)
