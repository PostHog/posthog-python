import calendar
import datetime
import hashlib
import json
import logging
import math
import re
import warnings
from decimal import Decimal
from enum import Enum
from functools import lru_cache as _lru_cache
from typing import NamedTuple, NoReturn, Optional

from posthog import utils as utils
from posthog.types import FlagValue, UnresolvedFlagReason
from posthog.utils import convert_to_datetime_aware
from posthog.utils import is_valid_regex as is_valid_regex

__LONG_SCALE__ = float(0xFFFFFFFFFFFFFFF)

log = logging.getLogger("posthog")

# Tracks (flag_key, reason) pairs already warned about for malformed flag
# dependency conditions, so the warning fires at most once per process instead
# of on every get_feature_flag call. A plain set is safe under the GIL; a rare
# duplicate warning from a race is acceptable.
_warned_malformed_flag_dependencies: set = set()

NONE_VALUES_ALLOWED_OPERATORS = ["is_not"]


def _warn_malformed_flag_dependency_once(flag_key, reason):
    """Emit a throttled warning for a malformed flag dependency condition.

    Mirrors the server-side Rust evaluator, which logs before treating the
    condition as not matching, so customers can discover broken flag
    definitions locally. Deduplicated on (flag_key, reason) to avoid spamming
    hot evaluation paths.
    """
    key = (flag_key, reason)
    if key in _warned_malformed_flag_dependencies:
        return
    _warned_malformed_flag_dependencies.add(key)
    log.warning(
        f"Flag dependency condition on '{flag_key or 'unknown'}' is malformed "
        f"({reason}); treating as not matching during local evaluation. "
        f"Fix this condition in the PostHog UI."
    )


class ConditionMatch(Enum):
    """Outcome of evaluating a single condition group.

    OUT_OF_ROLLOUT_BOUND means the group's property filters matched (or there were none)
    but the rollout percentage excluded the user — the only case that triggers a flag's
    ``early_exit`` short-circuit. Mirrors the server-side (Rust) engine's match cases.
    """

    MATCH = "match"
    NO_MATCH = "no_match"
    OUT_OF_ROLLOUT_BOUND = "out_of_rollout_bound"


# All operators supported by match_property, grouped by category.
EQUALITY_OPERATORS = ("exact", "is_not", "is_set", "is_not_set")
STRING_OPERATORS = (
    "icontains",
    "not_icontains",
    "regex",
    "not_regex",
    "starts_with",
    "not_starts_with",
    "ends_with",
    "not_ends_with",
)
NUMERIC_OPERATORS = ("gt", "gte", "lt", "lte")
DATE_OPERATORS = ("is_date_before", "is_date_after")
SEMVER_COMPARISON_OPERATORS = (
    "semver_eq",
    "semver_neq",
    "semver_gt",
    "semver_gte",
    "semver_lt",
    "semver_lte",
)
SEMVER_RANGE_OPERATORS = ("semver_tilde", "semver_caret", "semver_wildcard")
SEMVER_OPERATORS = SEMVER_COMPARISON_OPERATORS + SEMVER_RANGE_OPERATORS

PROPERTY_OPERATORS = (
    EQUALITY_OPERATORS
    + STRING_OPERATORS
    + NUMERIC_OPERATORS
    + DATE_OPERATORS
    + SEMVER_OPERATORS
)


class InconclusiveMatchError(Exception):
    """
    Raised when local evaluation can't determine a flag's value. ``reason`` says
    why, as reported by ``FeatureFlagEvaluations.unresolved_flags``.

    Attributes:
        reason: Why local evaluation couldn't resolve the flag.
    """

    reason: UnresolvedFlagReason = UnresolvedFlagReason.UNSUPPORTED_DEFINITION

    def __init__(self, *args, reason: Optional[UnresolvedFlagReason] = None):
        super().__init__(*args)
        if reason is not None:
            self.reason = reason


class RequiresServerEvaluation(Exception):
    """
    Raised when feature flag evaluation requires server-side data that is not
    available locally (e.g., static cohorts).

    This error should propagate immediately to trigger API fallback, unlike
    InconclusiveMatchError which allows trying other conditions.

    Attributes:
        reason: Why the flag needs server-side evaluation.
    """

    reason: UnresolvedFlagReason = UnresolvedFlagReason.UNSUPPORTED_DEFINITION

    def __init__(self, *args, reason: Optional[UnresolvedFlagReason] = None):
        super().__init__(*args)
        if reason is not None:
            self.reason = reason


# When several causes apply to one flag, the spec reports the first reason in this
# order, so every SDK picks the same reason whatever order it inspects filters in.
_UNRESOLVED_REASON_PRECEDENCE = (
    UnresolvedFlagReason.EXPERIENCE_CONTINUITY,
    UnresolvedFlagReason.UNSUPPORTED_DEFINITION,
    UnresolvedFlagReason.UNRESOLVED_DEPENDENCY,
    UnresolvedFlagReason.MISSING_CONTEXT,
)


def _pick_unresolved_reason(
    current: Optional[UnresolvedFlagReason], new: UnresolvedFlagReason
) -> UnresolvedFlagReason:
    if current is None:
        return new
    return min(current, new, key=_UNRESOLVED_REASON_PRECEDENCE.index)


# This function takes a bucketing value and a feature flag key and returns a float between 0 and 1.
# Given the same bucketing value and key, it'll always return the same float. These floats are
# uniformly distributed between 0 and 1, so if we want to show this feature to 20% of traffic
# we can do _hash(key, bucketing_value) < 0.2
def _hash(
    key: str, bucketing_value: str, salt: str = "", separator: str = "."
) -> float:
    hash_key = f"{key}{separator}{bucketing_value}{salt}"
    hash_val = int(hashlib.sha1(hash_key.encode("utf-8")).hexdigest()[:15], 16)
    return hash_val / __LONG_SCALE__


def _holdout_hash(bucketing_value: str) -> float:
    """Hash a bucketing value for holdout membership, matching the server.

    The separator is the whole point: the server hashes `holdout-<value>`, while flag
    rollout hashing joins with a dot. Taking the default separator here would still look
    uniform and deterministic while holding out a different set of people than the server.
    """
    return _hash("holdout", bucketing_value, separator="-")


def _get_holdout_variant(flag, bucketing_value) -> Optional[str]:
    """The `holdout-<id>` variant this value is held out into, or None.

    Mirrors the server's evaluation order: a held-out value never reaches the flag's
    release conditions, so callers check this before matching any condition.
    """
    holdout = (flag.get("filters") or {}).get("holdout")
    if not holdout:
        return None

    exclusion_percentage = holdout.get("exclusion_percentage")
    holdout_id = holdout.get("id")
    if exclusion_percentage is None or holdout_id is None:
        return None

    # The server clamps out-of-range percentages rather than rejecting them, and treats
    # 100 as "everyone" without hashing, so a 100% holdout can't miss on a hash boundary.
    percentage = min(max(float(exclusion_percentage), 0.0), 100.0)
    if percentage != 100.0 and _holdout_hash(bucketing_value) > percentage / 100:
        return None

    return f"holdout-{holdout_id}"


def get_matching_variant(flag, bucketing_value):
    hash_value = _hash(flag["key"], bucketing_value, salt="variant")
    for variant in variant_lookup_table(flag):
        if hash_value >= variant["value_min"] and hash_value < variant["value_max"]:
            return variant["key"]
    return None


def variant_lookup_table(feature_flag):
    lookup_table = []
    value_min = 0
    multivariates = ((feature_flag.get("filters") or {}).get("multivariate") or {}).get(
        "variants"
    ) or []
    for variant in multivariates:
        value_max = value_min + variant["rollout_percentage"] / 100
        lookup_table.append(
            {"value_min": value_min, "value_max": value_max, "key": variant["key"]}
        )
        value_min = value_max
    return lookup_table


def _flag_dependency_shape_problem(prop) -> Optional[str]:
    """Describe why a flag dependency condition is malformed, or return None.

    Shared by local evaluation and the load-time classifier, so the two agree."""
    operator = prop.get("operator", "exact")
    if operator != "flag_evaluates_to":
        return f"invalid operator '{operator}'"
    # Test `is None`, not truthiness: `False` is a valid expected value (the case
    # flag dependencies exist for).
    if not prop.get("key") or prop.get("value") is None:
        return "missing key or value"
    return None


def evaluate_flag_dependency(
    property,
    flags_by_key,
    evaluation_cache,
    distinct_id,
    properties,
    cohort_properties,
    device_id=None,
    property_matching_version: int = 1,
):
    """
    Evaluate a flag dependency condition under local evaluation.

    The dependency_chain only establishes the order in which flags are evaluated
    and cached (the referenced flag itself is always the last member). The
    outcome is decided solely by comparing the referenced flag's evaluated value
    against the condition's expected value; ancestors influence it only through
    the referenced flag's own recursive evaluation.

    Args:
        property: Flag property with type="flag" and dependency_chain
        flags_by_key: Dictionary of all flags by their key
        evaluation_cache: Cache for storing evaluation results
        distinct_id: The distinct ID being evaluated
        properties: Person properties for evaluation
        cohort_properties: Cohort properties for evaluation
        device_id: The device ID for bucketing (optional)

    Returns:
        bool: Whether the referenced flag's evaluated value matches the
            condition's expected value. A malformed condition shape (wrong
            operator, missing key, or missing value) is a definitive local
            no-match and returns False, mirroring the server-side evaluator.

    Raises:
        InconclusiveMatchError: If the chain cannot be conclusively evaluated
            locally (referenced flag missing, an inconclusive dependency,
            circular dependency, or missing evaluation context).
    """
    if flags_by_key is None or evaluation_cache is None:
        # Cannot evaluate flag dependencies without required context
        raise InconclusiveMatchError(
            f"Cannot evaluate flag dependency on '{property.get('key', 'unknown')}' without flags_by_key and evaluation_cache",
            reason=UnresolvedFlagReason.UNSUPPORTED_DEFINITION,
        )

    # Validate the condition shape before walking the chain. A malformed shape
    # is a definitive no-match (return False), mirroring the server-side Rust
    # evaluator (match_flag_value_to_flag_filter). Raising InconclusiveMatchError
    # here would force a billable /flags network fallback on every evaluation of
    # the affected flag.
    flag_key = property.get("key")
    expected_value = property.get("value")

    shape_problem = _flag_dependency_shape_problem(property)
    if shape_problem is not None:
        _warn_malformed_flag_dependency_once(flag_key, shape_problem)
        return False

    dependency_chain = _get_dependency_chain(property)
    _assert_dependency_definitions_loaded(
        dependency_chain, flags_by_key, evaluation_cache
    )
    _evaluate_dependency_chain(
        dependency_chain,
        flags_by_key,
        evaluation_cache,
        distinct_id,
        properties,
        cohort_properties,
        device_id,
        property_matching_version,
    )

    # The condition matches iff the referenced flag's value matches the expected
    # value. None means inconclusive or not evaluated — distinct from a
    # definitive False, which must be allowed to match `expected_value=False`.
    actual_value = evaluation_cache.get(flag_key)
    if actual_value is None:
        # A referenced flag that has no loaded definition or is missing from the
        # chain is a definition problem, not an unresolved dependency.
        reason = (
            UnresolvedFlagReason.UNRESOLVED_DEPENDENCY
            if flag_key in flags_by_key and flag_key in evaluation_cache
            else UnresolvedFlagReason.UNSUPPORTED_DEFINITION
        )
        raise InconclusiveMatchError(
            f"Flag dependency '{flag_key}' was inconclusive or not evaluated",
            reason=reason,
        )
    return matches_dependency_value(expected_value, actual_value)


def _get_dependency_chain(property):
    # Check if dependency_chain is present - it should always be provided for flag dependencies
    if "dependency_chain" not in property:
        # Missing dependency_chain indicates malformed server data
        raise InconclusiveMatchError(
            f"Flag dependency property for '{property.get('key', 'unknown')}' is missing required 'dependency_chain' field",
            reason=UnresolvedFlagReason.UNSUPPORTED_DEFINITION,
        )

    flag_key = property.get("key")
    dependency_chain = property["dependency_chain"]

    if not isinstance(dependency_chain, list):
        raise InconclusiveMatchError(
            f"Flag dependency property for '{flag_key}' has an invalid 'dependency_chain' field",
            reason=UnresolvedFlagReason.UNSUPPORTED_DEFINITION,
        )

    # Handle circular dependency (empty chain means circular)
    if len(dependency_chain) == 0:
        log.debug(f"Circular dependency detected for flag: {property.get('key')}")
        raise InconclusiveMatchError(
            f"Circular dependency detected for flag '{property.get('key', 'unknown')}'",
            reason=UnresolvedFlagReason.UNSUPPORTED_DEFINITION,
        )

    return dependency_chain


def _assert_dependency_definitions_loaded(
    dependency_chain, flags_by_key, evaluation_cache
):
    # A dependency with no definition outranks one that is only inconclusive, so
    # check the whole chain before evaluating any of it.
    missing_dependency = next(
        (
            key
            for key in dependency_chain
            if key not in evaluation_cache and not flags_by_key.get(key)
        ),
        None,
    )
    if missing_dependency is not None:
        evaluation_cache[missing_dependency] = None
        raise InconclusiveMatchError(
            f"Cannot evaluate flag dependency '{missing_dependency}' - flag not found in local flags",
            reason=UnresolvedFlagReason.UNSUPPORTED_DEFINITION,
        )


def _evaluate_dependency_chain(
    dependency_chain,
    flags_by_key,
    evaluation_cache,
    distinct_id,
    properties,
    cohort_properties,
    device_id,
    property_matching_version,
):
    # Evaluate and cache each flag in the chain; members already cached are
    # skipped. This does not decide the outcome — it only populates the cache.
    for dep_flag_key in dependency_chain:
        if dep_flag_key in evaluation_cache:
            continue
        _evaluate_dependency(
            dep_flag_key,
            flags_by_key,
            evaluation_cache,
            distinct_id,
            properties,
            cohort_properties,
            device_id,
            property_matching_version,
        )


def _evaluate_dependency(
    dep_flag_key,
    flags_by_key,
    evaluation_cache,
    distinct_id,
    properties,
    cohort_properties,
    device_id,
    property_matching_version,
):
    dep_flag = flags_by_key[dep_flag_key]

    # Same order as client._compute_flag_locally: an inactive flag is false
    # whatever its experience continuity setting.
    if not dep_flag.get("active"):
        evaluation_cache[dep_flag_key] = False
        return

    if dep_flag.get("ensure_experience_continuity"):
        evaluation_cache[dep_flag_key] = None
        raise InconclusiveMatchError(
            f"Cannot evaluate flag dependency '{dep_flag_key}' - flag has experience continuity enabled",
            reason=UnresolvedFlagReason.UNRESOLVED_DEPENDENCY,
        )

    # Recursively evaluate the dependency
    try:
        dep_flag_filters = dep_flag.get("filters") or {}
        dep_aggregation_group_type_index = dep_flag_filters.get(
            "aggregation_group_type_index"
        )
        if dep_aggregation_group_type_index is not None:
            # Group flags should continue bucketing by the group key
            # from the current evaluation context.
            dep_bucketing_value = distinct_id
        else:
            dep_bucketing_value = resolve_bucketing_value(
                dep_flag, distinct_id, device_id
            )
        dep_result = match_feature_flag_properties(
            dep_flag,
            distinct_id,
            properties,
            cohort_properties=cohort_properties,
            flags_by_key=flags_by_key,
            evaluation_cache=evaluation_cache,
            device_id=device_id,
            property_matching_version=property_matching_version,
            bucketing_value=dep_bucketing_value,
        )
        evaluation_cache[dep_flag_key] = dep_result
    except RequiresServerEvaluation as e:
        # The dependent flag needs the server too, so its later conditions must
        # not resolve it locally. Nothing is cached, so another filter on this
        # dependency also raises this error.
        raise RequiresServerEvaluation(
            f"Cannot evaluate flag dependency '{dep_flag_key}': {e}",
            reason=UnresolvedFlagReason.UNRESOLVED_DEPENDENCY,
        ) from e
    except InconclusiveMatchError as e:
        # If we can't evaluate a dependency, store None and propagate the error
        evaluation_cache[dep_flag_key] = None
        raise InconclusiveMatchError(
            f"Cannot evaluate flag dependency '{dep_flag_key}': {e}",
            reason=UnresolvedFlagReason.UNRESOLVED_DEPENDENCY,
        ) from e


def matches_dependency_value(expected_value, actual_value):
    """
    Check if the actual flag value matches the expected dependency value.

    This follows the same logic as the C# MatchesDependencyValue function:
    - String variant case: check for exact match or boolean true
    - Boolean case: must match expected boolean value

    Args:
        expected_value: The expected value from the property
        actual_value: The actual value returned by the flag evaluation

    Returns:
        bool: True if the values match according to flag dependency rules
    """
    # String variant case - check for exact match or boolean true
    if isinstance(actual_value, str) and len(actual_value) > 0:
        if isinstance(expected_value, bool):
            # Any variant matches boolean true
            return expected_value
        elif isinstance(expected_value, str):
            # variants are case-sensitive, hence our comparison is too
            return actual_value == expected_value
        else:
            return False

    # Boolean case - must match expected boolean value
    elif isinstance(actual_value, bool) and isinstance(expected_value, bool):
        return actual_value == expected_value

    # Default case
    return False


def resolve_bucketing_value(flag, distinct_id, device_id=None):
    """Resolve the bucketing value for a flag based on its bucketing_identifier setting.

    Returns:
        The appropriate identifier string to use for hashing/bucketing.

    Raises:
        InconclusiveMatchError: If the flag requires device_id but none was provided.
    """
    flag_filters = flag.get("filters") or {}
    bucketing_identifier = flag.get("bucketing_identifier") or flag_filters.get(
        "bucketing_identifier"
    )
    if bucketing_identifier == "device_id":
        if not device_id:
            raise InconclusiveMatchError(
                "Flag requires device_id for bucketing but none was provided",
                reason=UnresolvedFlagReason.MISSING_CONTEXT,
            )
        return device_id
    return distinct_id


def match_feature_flag_properties(
    flag,
    distinct_id,
    properties,
    *,
    cohort_properties=None,
    flags_by_key=None,
    evaluation_cache=None,
    device_id=None,
    bucketing_value=None,
    group_type_mapping=None,
    groups=None,
    group_properties=None,
    property_matching_version: int = 1,
) -> FlagValue:
    if bucketing_value is None:
        warnings.warn(
            "Calling match_feature_flag_properties() without bucketing_value is deprecated. "
            "Pass bucketing_value explicitly. This fallback will be removed in a future major release.",
            DeprecationWarning,
            stacklevel=2,
        )
        bucketing_value = resolve_bucketing_value(flag, distinct_id, device_id)

    flag_filters = flag.get("filters") or {}

    if flag_filters.get("feature_enrollment") is True:
        return _match_feature_enrollment(flag, flag_filters, properties)

    # Holdouts are evaluated before release conditions, so a held-out value is excluded
    # from the flag's targeting entirely rather than being bucketed into a variant.
    holdout_variant = _get_holdout_variant(flag, bucketing_value)
    if holdout_variant is not None:
        return holdout_variant

    return _match_flag_conditions(
        flag,
        flag_filters,
        distinct_id,
        properties,
        cohort_properties or {},
        flags_by_key,
        evaluation_cache,
        device_id,
        bucketing_value,
        group_type_mapping or {},
        groups or {},
        group_properties or {},
        property_matching_version,
    )


def _match_flag_conditions(
    flag,
    flag_filters,
    distinct_id,
    properties,
    cohort_properties,
    flags_by_key,
    evaluation_cache,
    device_id,
    bucketing_value,
    group_type_mapping,
    groups,
    group_properties,
    property_matching_version,
) -> FlagValue:
    flag_conditions = flag_filters.get("groups") or []
    flag_aggregation = flag_filters.get("aggregation_group_type_index")
    early_exit_enabled = flag_filters.get("early_exit")
    inconclusive_reason: Optional[UnresolvedFlagReason] = None
    valid_variant_keys = _get_valid_variant_keys(flag_filters)

    for condition in flag_conditions:
        try:
            match_result, value = _match_flag_condition(
                flag,
                distinct_id,
                condition,
                properties,
                cohort_properties,
                flags_by_key,
                evaluation_cache,
                device_id,
                bucketing_value,
                group_type_mapping,
                groups,
                group_properties,
                property_matching_version,
                flag_aggregation,
                valid_variant_keys,
            )
        except RequiresServerEvaluation as e:
            _enrich_server_evaluation_reason(
                e,
                flag,
                flags_by_key,
                cohort_properties,
                group_type_mapping,
            )
            raise
        except InconclusiveMatchError as e:
            # Evaluation error (bad regex, invalid date, missing property, etc.)
            # Track that we had an inconclusive match, but try other conditions
            inconclusive_reason = _pick_unresolved_reason(inconclusive_reason, e.reason)
            continue
        if match_result == ConditionMatch.MATCH:
            return value
        elif early_exit_enabled and match_result == ConditionMatch.OUT_OF_ROLLOUT_BOUND:
            # The condition's property filters (if any) matched and only the rollout check
            # failed, so re-evaluating later groups can't change the outcome. If an earlier
            # condition was inconclusive, stop here but preserve that result for fallback.
            return _unmatched_flag_result(inconclusive_reason)

    return _unmatched_flag_result(inconclusive_reason)


def _unmatched_flag_result(inconclusive_reason) -> bool:
    if inconclusive_reason is not None:
        raise InconclusiveMatchError(
            "Can't determine if feature flag is enabled or not with given properties",
            reason=inconclusive_reason,
        )

    # We can only return False when either all conditions are False, or
    # no condition was inconclusive.
    return False


def _get_condition_context(
    flag,
    condition,
    flag_aggregation,
    properties,
    bucketing_value,
    group_type_mapping,
    groups,
    group_properties,
):
    # Per-condition aggregation overrides only when the condition explicitly
    # sets its own aggregation_group_type_index (mixed targeting).
    # When absent, use the properties/bucketing already resolved by the caller.
    condition_aggregation = condition.get(
        "aggregation_group_type_index", flag_aggregation
    )

    # Mixed-override path: condition-level aggregation differs from flag-level.
    # This assumes flag-level aggregation is None for mixed flags.
    if condition_aggregation != flag_aggregation:
        if condition_aggregation is not None:
            group_name = group_type_mapping.get(str(condition_aggregation))
            if not group_name or group_name not in groups:
                log.debug(
                    "Skipping group condition for flag '%s': group type index %s not available",
                    flag.get("key", ""),
                    condition_aggregation,
                )
                return None
            if group_name not in group_properties:
                raise InconclusiveMatchError(
                    "Group properties were not passed",
                    reason=UnresolvedFlagReason.MISSING_CONTEXT,
                )
            effective_properties = group_properties[group_name]
            effective_bucketing = groups[group_name]
        else:
            effective_properties = properties
            effective_bucketing = bucketing_value
    else:
        effective_properties = properties
        effective_bucketing = bucketing_value

    return effective_properties, effective_bucketing


def _match_flag_condition(
    flag,
    distinct_id,
    condition,
    properties,
    cohort_properties,
    flags_by_key,
    evaluation_cache,
    device_id,
    bucketing_value,
    group_type_mapping,
    groups,
    group_properties,
    property_matching_version,
    flag_aggregation,
    valid_variant_keys,
):
    context = _get_condition_context(
        flag,
        condition,
        flag_aggregation,
        properties,
        bucketing_value,
        group_type_mapping,
        groups,
        group_properties,
    )
    if context is None:
        return ConditionMatch.NO_MATCH, None
    effective_properties, effective_bucketing = context
    match_result = is_condition_match(
        flag,
        distinct_id,
        condition,
        effective_properties,
        cohort_properties,
        flags_by_key,
        evaluation_cache,
        bucketing_value=effective_bucketing,
        device_id=device_id,
        property_matching_version=property_matching_version,
    )
    if match_result == ConditionMatch.MATCH:
        variant_override = condition.get("variant")
        if variant_override and variant_override in valid_variant_keys:
            variant = variant_override
        else:
            variant = get_matching_variant(flag, effective_bucketing)
        return match_result, variant or True
    return match_result, None


def _enrich_server_evaluation_reason(
    e, flag, flags_by_key, cohort_properties, group_type_mapping
):
    # Static cohort or other missing server-side data - must fallback to API.
    # The loop stops here, so a filter it did not reach can still carry a
    # higher-precedence reason in the flag's definition.
    try:
        definition_reason = _get_definition_unresolved_reason(
            flag, flags_by_key or {}, cohort_properties, group_type_mapping
        )
    except Exception:
        # A malformed definition must not stop the fallback to the server.
        definition_reason = None
    if definition_reason is not None:
        e.reason = _pick_unresolved_reason(e.reason, definition_reason)


def _get_valid_variant_keys(flag_filters):
    # Some filters can be explicitly set to null, which require accessing variants like so
    flag_variants = (flag_filters.get("multivariate") or {}).get("variants") or []
    return [variant["key"] for variant in flag_variants]


def _match_feature_enrollment(flag, flag_filters, properties) -> bool:
    enrollment_key = f"$feature_enrollment/{flag.get('key')}"
    if flag_filters.get("aggregation_group_type_index") is not None:
        raise InconclusiveMatchError(
            "Group flag has feature enrollment, which reads person properties",
            reason=UnresolvedFlagReason.UNSUPPORTED_DEFINITION,
        )
    if enrollment_key not in properties:
        raise InconclusiveMatchError(
            f"Flag has feature enrollment and '{enrollment_key}' was not passed",
            reason=UnresolvedFlagReason.MISSING_CONTEXT,
        )
    enrollment = properties[enrollment_key]
    return enrollment is True or enrollment == "true"


def _match_condition_filter(
    prop,
    properties,
    cohort_properties,
    flags_by_key,
    evaluation_cache,
    distinct_id,
    *,
    device_id,
    property_matching_version: int,
) -> bool:
    property_type = prop.get("type")
    if property_type == "cohort":
        return match_cohort(
            prop,
            properties,
            cohort_properties,
            flags_by_key,
            evaluation_cache,
            distinct_id,
            device_id=device_id,
            property_matching_version=property_matching_version,
        )
    if property_type == "flag":
        return evaluate_flag_dependency(
            prop,
            flags_by_key,
            evaluation_cache,
            distinct_id,
            properties,
            cohort_properties,
            device_id=device_id,
            property_matching_version=property_matching_version,
        )
    return match_property(prop, properties, property_matching_version)


def is_condition_match(
    feature_flag,
    distinct_id,
    condition,
    properties,
    cohort_properties,
    flags_by_key=None,
    evaluation_cache=None,
    *,
    bucketing_value,
    device_id=None,
    property_matching_version: int = 1,
) -> ConditionMatch:
    rollout_percentage = condition.get("rollout_percentage")
    if len(condition.get("properties") or []) > 0:
        if not _match_condition_properties(
            feature_flag,
            distinct_id,
            condition,
            properties,
            cohort_properties,
            flags_by_key,
            evaluation_cache,
            device_id,
            property_matching_version,
        ):
            return ConditionMatch.NO_MATCH

        if rollout_percentage is None:
            return ConditionMatch.MATCH

    # Property filters (if any) matched; only the rollout check remains. A failure here means
    # the user was targeted but excluded by rollout — the server-side engine's OutOfRolloutBound.
    if rollout_percentage is not None and _hash(
        feature_flag["key"], bucketing_value
    ) > (rollout_percentage / 100):
        return ConditionMatch.OUT_OF_ROLLOUT_BOUND

    return ConditionMatch.MATCH


def _match_condition_properties(
    feature_flag,
    distinct_id,
    condition,
    properties,
    cohort_properties,
    flags_by_key,
    evaluation_cache,
    device_id,
    property_matching_version,
) -> bool:
    # Filters in a condition have no defined order, so an inconclusive filter does
    # not stop the loop: a later filter can still rule the condition out or carry a
    # higher-precedence reason.
    inconclusive_reason: Optional[UnresolvedFlagReason] = None
    server_evaluation_error: Optional[RequiresServerEvaluation] = None
    for prop in condition.get("properties"):
        try:
            matches = _match_condition_filter(
                prop,
                properties,
                cohort_properties,
                flags_by_key,
                evaluation_cache,
                distinct_id,
                device_id=device_id,
                property_matching_version=property_matching_version,
            )
        except RequiresServerEvaluation as e:
            # Fall back to the server only when this is the first filter that could
            # not be checked, as when the loop stopped there. After another
            # inconclusive filter, later conditions must still be able to resolve
            # the flag locally.
            if inconclusive_reason is None:
                server_evaluation_error = e
            inconclusive_reason = _pick_unresolved_reason(inconclusive_reason, e.reason)
            continue
        except InconclusiveMatchError as e:
            inconclusive_reason = _pick_unresolved_reason(inconclusive_reason, e.reason)
            continue
        except Exception:
            # After an inconclusive filter, an unexpected error ends the condition
            # as inconclusive, so later conditions can still resolve the flag.
            if inconclusive_reason is None:
                raise
            break
        if not matches:
            return False

    _raise_condition_errors(feature_flag, server_evaluation_error, inconclusive_reason)
    return True


def _raise_condition_errors(feature_flag, server_evaluation_error, inconclusive_reason):
    # Re-raise the original error type: match_feature_flag_properties stops at it
    # and falls back to the server instead of trying later conditions.
    if server_evaluation_error is not None:
        raise server_evaluation_error
    if inconclusive_reason is not None:
        raise InconclusiveMatchError(
            f"Can't determine if a condition of flag {feature_flag.get('key')} matches",
            reason=inconclusive_reason,
        )


# Raised when an operator passes the PROPERTY_OPERATORS gate but has no dispatch
# branch in match_property. Distinct from the unknown-operator rejection at the top
# of the function so the dispatch-completeness test can tell the two apart.
_UNHANDLED_OPERATOR_MESSAGE = "has no match_property branch"
_ASCII_LOWER_TRANSLATION = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)


# The value helpers below convert both filter values and values from the evaluation
# call. ``reason`` names the owner of the value: UNSUPPORTED_DEFINITION for a filter
# value, MISSING_CONTEXT for a value the call passed.
def _format_json_float(value: float, reason: UnresolvedFlagReason) -> str:
    if not math.isfinite(value):
        raise InconclusiveMatchError(
            "Non-finite property values cannot be represented by the flags service",
            reason=reason,
        )

    representation = repr(value)
    if "e" not in representation:
        return representation

    mantissa, exponent_text = representation.split("e")
    exponent = int(exponent_text)
    if -6 < exponent < 0:
        return format(Decimal(representation), "f")

    sign = "+" if exponent >= 0 else ""
    return f"{mantissa}e{sign}{exponent}"


def _json_value_to_string(value, reason: UnresolvedFlagReason) -> str:
    if isinstance(value, list):
        return (
            "[" + ",".join(_json_value_to_string(item, reason) for item in value) + "]"
        )
    if isinstance(value, dict):
        return _json_object_to_string(value, reason)

    return _json_scalar_to_string(value, reason)


def _json_scalar_to_string(value, reason: UnresolvedFlagReason) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _format_json_float(value, reason)
    raise InconclusiveMatchError(
        f"Property value of type {type(value).__name__} is not JSON-compatible",
        reason=reason,
    )


def _json_object_to_string(value: dict, reason: UnresolvedFlagReason) -> str:
    if not all(isinstance(key, str) for key in value):
        raise InconclusiveMatchError(
            "Property object keys must be strings for local evaluation",
            reason=reason,
        )
    items = (
        f"{json.dumps(key, ensure_ascii=False)}:{_json_value_to_string(value[key], reason)}"
        for key in sorted(value)
    )
    return "{" + ",".join(items) + "}"


def _value_to_string(value, reason: UnresolvedFlagReason) -> str:
    if isinstance(value, str):
        return value
    return _json_value_to_string(value, reason)


def _ascii_lower(value, reason: UnresolvedFlagReason) -> str:
    return _value_to_string(value, reason).translate(_ASCII_LOWER_TRANSLATION)


def _is_truthy_or_falsy_property_value(value) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        return value.lower() in ("true", "false")
    if isinstance(value, list):
        return all(_is_truthy_or_falsy_property_value(item) for item in value)
    return False


def _is_truthy_property_value(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == "true"
    if isinstance(value, list):
        return all(_is_truthy_property_value(item) for item in value)
    return False


_NUMERIC_STRING_PATTERN = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
)


def _parse_numeric_value(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    if isinstance(value, str) and not _NUMERIC_STRING_PATTERN.fullmatch(value):
        return None
    try:
        parsed = float(value)
    except (ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


class _RegexMetadata(NamedTuple):
    regex: re.Pattern[str]
    uses_word_class: bool
    uses_space_class: bool
    uses_end_anchor: bool
    uses_quantifier_or_alternation: bool


def _defer_regex() -> NoReturn:
    raise InconclusiveMatchError(
        "Regex requires server evaluation",
        reason=UnresolvedFlagReason.UNSUPPORTED_DEFINITION,
    )


class _RegexTranslator:
    """Scan Python/Rust shared syntax, retaining the metadata needed at match time."""

    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        self.translated: list[str] = []
        self.class_start: Optional[int] = None
        self.previous = ""
        self.group_has_quantifier: list[bool] = []
        self.closed_group_has_quantifier = False
        self.uses_word_class = False
        self.uses_space_class = False
        self.uses_end_anchor = False
        self.uses_quantifier_or_alternation = False
        self.index = 0

    def translate(self) -> str:
        while self.index < len(self.pattern):
            character = self.pattern[self.index]
            if 0xD800 <= ord(character) <= 0xDFFF:
                _defer_regex()
            if self.class_start is None:
                self._scan_group(character)
            self._translate_character(character)
            self.index += 1
        return "".join(self.translated)

    def _scan_group(self, character: str) -> None:
        self.uses_end_anchor |= character == "$"
        self.uses_quantifier_or_alternation |= character in "*+?{|"
        if character == "(":
            self.group_has_quantifier.append(False)
        elif character == ")":
            self.closed_group_has_quantifier = (
                self.group_has_quantifier.pop() if self.group_has_quantifier else False
            )
            if self.group_has_quantifier:
                self.group_has_quantifier[-1] |= self.closed_group_has_quantifier
        elif character in "*+?{":
            # Strict anchors can turn nested repetition into expensive backtracking.
            if self.previous == ")" and self.closed_group_has_quantifier:
                _defer_regex()
            if self.group_has_quantifier:
                self.group_has_quantifier[-1] = True

    def _translate_character(self, character: str) -> None:
        if character == "\\":
            self._translate_escape()
        elif character == "[":
            if self.class_start is not None:
                _defer_regex()
            self.class_start = self.index
            self.translated.append(character)
        elif character == "]" and self.class_start is not None:
            self._close_class()
        elif self.class_start is not None:
            self._translate_class_character(character)
        else:
            self._translate_outside_class(character)

    def _translate_escape(self) -> None:
        self.index += 1
        if self.index == len(self.pattern):
            self.translated.append("\\")
            return
        escaped = self.pattern[self.index]
        if escaped == "x":
            self._translate_hex_escape()
            return
        if escaped in "bB<>" and self.class_start is None:
            # Even short ASCII word-boundary searches can exceed the service's step limit.
            _defer_regex()
        self._assert_escape_supported(escaped)
        self.uses_word_class |= escaped in "wW"
        self.uses_space_class |= escaped in "sS"
        self.translated.append("\\" + escaped)
        self.previous = "literal"

    def _translate_hex_escape(self) -> None:
        digits = self.pattern[self.index + 1 : self.index + 3]
        if len(digits) != 2 or any(c not in "0123456789abcdefABCDEF" for c in digits):
            _defer_regex()
        self.translated.append("\\x" + digits)
        # The outer scan advances the last character of the escape.
        self.index += 2
        self.previous = "literal"

    def _assert_escape_supported(self, escaped: str) -> None:
        if escaped not in "dDsSwWbfnrt":
            if not (0x20 <= ord(escaped) <= 0x7E and not escaped.isalnum()):
                _defer_regex()
            if self.class_start is not None and escaped not in r"\-[]^.$*+?(){}|":
                _defer_regex()

    def _close_class(self) -> None:
        assert self.class_start is not None
        content_start = self.class_start + (
            2 if self.pattern[self.class_start + 1 : self.class_start + 2] == "^" else 1
        )
        if self.index == content_start:
            _defer_regex()
        self.class_start = None
        self.previous = "literal"
        self.translated.append("]")

    def _translate_class_character(self, character: str) -> None:
        if self.pattern[self.index : self.index + 2] in ("&&", "--", "~~"):
            _defer_regex()
        # Rust and Python disagree on shorthand endpoints in ranges.
        if (
            character == "-"
            and self.index + 1 < len(self.pattern)
            and self.pattern[self.index + 1] != "]"
        ):
            self._assert_range_supported()
        self.translated.append(character)

    def _assert_range_supported(self) -> None:
        if self.pattern[self.index + 1 : self.index + 3] in (
            r"\d",
            r"\D",
            r"\s",
            r"\S",
            r"\w",
            r"\W",
        ) or (
            self.index >= 2
            and self.pattern[self.index - 2] == "\\"
            and self.pattern[self.index - 1] in "dDsSwW"
        ):
            _defer_regex()

    def _translate_outside_class(self, character: str) -> None:
        if character == "(" and self.pattern[self.index + 1 : self.index + 2] == "?":
            if self.pattern[self.index + 2 : self.index + 3] != ":":
                _defer_regex()
            self.translated.append("(?:")
            self.index += 2
            self.previous = "("
        elif character == "{":
            self._translate_repetition()
        else:
            self._translate_literal_or_quantifier(character)

    def _translate_repetition(self) -> None:
        repetition = re.match(r"\{([0-9]+)(?:,([0-9]*))?\}", self.pattern[self.index :])
        if repetition is None or self.previous in (
            "",
            "|",
            "(",
            "^",
            "$",
            "empty group",
            "quantifier",
        ):
            _defer_regex()
        if any(len(n) > 10 or int(n) > 0xFFFFFFFF for n in repetition.groups() if n):
            _defer_regex()
        self.translated.append(repetition.group())
        self.index += len(repetition.group()) - 1
        if self.pattern[self.index + 1 : self.index + 2] in ("+", "{"):
            _defer_regex()
        self.previous = "quantifier"

    def _translate_literal_or_quantifier(self, character: str) -> None:
        if character in "*+?":
            if self.previous in ("^", "$", "empty group") or self.pattern[
                self.index + 1 : self.index + 2
            ] in ("+", "{"):
                _defer_regex()
            self.previous = "quantifier"
        else:
            self.previous = (
                "empty group"
                if character == ")" and self.previous in ("(", "empty group")
                else character
            )
        # Python's $ also matches before a final newline; Rust's default $ is strict.
        self.translated.append(r"\Z" if character == "$" else character)


@_lru_cache(maxsize=256)
def _compile_regex(pattern: str) -> Optional[_RegexMetadata]:
    """Cache only pattern metadata; exceptions are not retained by lru_cache."""
    # Successful re compilation alone does not establish Rust/fancy-regex compatibility.
    translator = _RegexTranslator(pattern)
    translated = translator.translate()
    try:
        regex = re.compile(translated)
    except re.error:
        return None
    except Exception as error:
        raise InconclusiveMatchError(
            "Regex compilation requires server evaluation",
            reason=UnresolvedFlagReason.UNSUPPORTED_DEFINITION,
        ) from error

    return _RegexMetadata(
        regex,
        translator.uses_word_class,
        translator.uses_space_class,
        translator.uses_end_anchor,
        translator.uses_quantifier_or_alternation,
    )


def _match_regex(pattern: str, value: str) -> Optional[bool]:
    """Return None for invalid shared syntax; raise inconclusive for unsupported cases."""
    if pattern == "":
        return True
    metadata = _compile_regex(pattern)
    if metadata is None:
        return None
    regex = metadata.regex

    _assert_regex_value_supported(metadata, value)
    try:
        return regex.search(value) is not None
    except Exception as error:
        raise InconclusiveMatchError(
            "Regex execution requires server evaluation",
            reason=UnresolvedFlagReason.UNSUPPORTED_DEFINITION,
        ) from error


def _assert_regex_value_supported(metadata: _RegexMetadata, value: str) -> None:
    # Tightening $ can expose backtracking on a final LF, including repeated alternatives.
    if (
        metadata.uses_end_anchor
        and metadata.uses_quantifier_or_alternation
        and value.endswith("\n")
    ):
        _defer_regex()

    # Unicode letters/decimal digits agree, but Rust's word class also includes marks and
    # joiners, while Python includes other numbers. Python's whitespace adds U+001C..U+001F.
    if not value.isascii():
        _assert_regex_unicode_supported(metadata, value)
    if metadata.uses_space_class and any(c in value for c in "\x1c\x1d\x1e\x1f"):
        _defer_regex()


def _assert_regex_unicode_supported(metadata: _RegexMetadata, value: str) -> None:
    if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        _defer_regex()
    if metadata.uses_word_class and any(
        not c.isascii() and not (c.isalpha() or c.isdecimal()) for c in value
    ):
        _defer_regex()


_TEXT_OPERATORS = (
    "icontains",
    "not_icontains",
    "starts_with",
    "not_starts_with",
    "ends_with",
    "not_ends_with",
)


def _parse_numeric_filter(value) -> float:
    parsed_value = _parse_numeric_value(value)
    if parsed_value is None:
        raise InconclusiveMatchError(
            "The numeric condition is not a finite number",
            reason=UnresolvedFlagReason.UNSUPPORTED_DEFINITION,
        )
    return parsed_value


def _parse_date_filter(value) -> datetime.datetime:
    try:
        parsed_date = relative_date_parse_for_feature_flag_matching(str(value))

        if not parsed_date:
            parsed_date = parse_datetime(str(value))
            parsed_date = convert_to_datetime_aware(parsed_date)
    except Exception as e:
        raise InconclusiveMatchError(
            "The date set on the flag is not a valid format",
            reason=UnresolvedFlagReason.UNSUPPORTED_DEFINITION,
        ) from e

    if not parsed_date:
        raise InconclusiveMatchError(
            "The date set on the flag is not a valid format",
            reason=UnresolvedFlagReason.UNSUPPORTED_DEFINITION,
        )
    return parsed_date


@_lru_cache(maxsize=1024)
def _assert_date_filter_supported(value: str) -> None:
    """Cache only valid filter values; lru_cache does not keep exceptions. Matching
    still parses each time, so relative dates resolve against the current time."""
    _parse_date_filter(value)


def _parse_semver_filter(operator, value) -> tuple:
    """Return the parsed version for a comparison operator, or the (lower, upper)
    bounds for a range operator."""
    try:
        if operator == "semver_tilde":
            return _tilde_bounds(str(value))
        if operator == "semver_caret":
            return _caret_bounds(str(value))
        if operator == "semver_wildcard":
            return _wildcard_bounds(str(value))
        return parse_semver(value)
    except (ValueError, TypeError):
        raise InconclusiveMatchError(
            f"Flag semver value '{value}' is not valid for operator {operator}",
            reason=UnresolvedFlagReason.UNSUPPORTED_DEFINITION,
        )


def _assert_filter_supported(property) -> None:
    """Raise an unsupported_definition error when local evaluation can't use a
    property filter, whatever the call passes: an unknown operator, or a filter
    value the operator can't parse."""
    operator = property.get("operator") or "exact"
    value = property.get("value")
    if operator not in PROPERTY_OPERATORS:
        raise InconclusiveMatchError(
            f"Unknown operator {operator}",
            reason=UnresolvedFlagReason.UNSUPPORTED_DEFINITION,
        )
    if operator in ("exact", "is_not"):
        for item in value if isinstance(value, list) else [value]:
            _value_to_string(item, UnresolvedFlagReason.UNSUPPORTED_DEFINITION)
    elif operator in _TEXT_OPERATORS:
        _value_to_string(value, UnresolvedFlagReason.UNSUPPORTED_DEFINITION)
    else:
        _assert_parsed_filter_supported(operator, value)


def _assert_parsed_filter_supported(operator, value) -> None:
    if operator in ("regex", "not_regex"):
        pattern = str(value)
        if pattern:
            _compile_regex(pattern)
    elif operator in NUMERIC_OPERATORS:
        _parse_numeric_filter(value)
    elif operator in DATE_OPERATORS:
        _assert_date_filter_supported(str(value))
    elif operator in SEMVER_OPERATORS:
        _parse_semver_filter(operator, value)


def match_property(
    property, property_values, property_matching_version: int = 1
) -> bool:
    try:
        return _match_property_value(
            property, property_values, property_matching_version
        )
    except InconclusiveMatchError as e:
        # An unusable filter outranks missing context, so check the filter before
        # blaming the context.
        if e.reason == UnresolvedFlagReason.MISSING_CONTEXT:
            _assert_filter_supported(property)
        raise


def _match_property_value(
    property, property_values, property_matching_version: int
) -> bool:
    # only looks for matches where key exists in override_property_values
    key = property.get("key")
    operator = property.get("operator") or "exact"
    value = property.get("value")

    if operator not in PROPERTY_OPERATORS:
        raise InconclusiveMatchError(
            f"Unknown operator {operator}",
            reason=UnresolvedFlagReason.UNSUPPORTED_DEFINITION,
        )

    if key not in property_values:
        raise InconclusiveMatchError(
            "can't match properties without a given property value",
            reason=UnresolvedFlagReason.MISSING_CONTEXT,
        )

    if operator in ("is_set", "is_not_set"):
        return operator == "is_set"

    override_value = property_values[key]

    if operator in NUMERIC_OPERATORS:
        return _match_numeric_property(operator, value, override_value)

    if _is_unmatchable_null(operator, override_value):
        return False

    if operator in ("exact", "is_not"):
        matched = _match_exact_property(
            value, override_value, property_matching_version
        )
        return matched if operator == "exact" else not matched

    return _match_scalar_property(operator, value, override_value)


def _is_unmatchable_null(operator, override_value) -> bool:
    return (
        operator not in NONE_VALUES_ALLOWED_OPERATORS
        and operator not in STRING_OPERATORS
        and operator != "exact"
        and override_value is None
    )


def _match_numeric_property(operator, value, override_value) -> bool:
    parsed_value = _parse_numeric_filter(value)
    parsed_override = _parse_numeric_value(override_value)
    if parsed_override is None:
        return False
    if operator == "gt":
        return parsed_override > parsed_value
    if operator == "gte":
        return parsed_override >= parsed_value
    if operator == "lt":
        return parsed_override < parsed_value
    return parsed_override <= parsed_value


def _match_exact_property(value, override_value, property_matching_version) -> bool:
    override_string = _value_to_string(
        override_value, UnresolvedFlagReason.MISSING_CONTEXT
    ).lower()
    # Empty filters retain recursive truthiness in both matching modes.
    if value == []:
        return _is_truthy_property_value(override_value)
    if property_matching_version != 2 and _is_truthy_or_falsy_property_value(value):
        return _is_truthy_property_value(value) == _is_truthy_property_value(
            override_value
        )

    if isinstance(value, list):
        return override_string in [
            _value_to_string(
                candidate, UnresolvedFlagReason.UNSUPPORTED_DEFINITION
            ).lower()
            for candidate in value
        ]
    return (
        _value_to_string(value, UnresolvedFlagReason.UNSUPPORTED_DEFINITION).lower()
        == override_string
    )


def _match_text_property(operator, value, override_value) -> bool:
    property_string = _ascii_lower(override_value, UnresolvedFlagReason.MISSING_CONTEXT)
    filter_string = _ascii_lower(value, UnresolvedFlagReason.UNSUPPORTED_DEFINITION)

    if operator in ("icontains", "not_icontains"):
        matched = filter_string in property_string
    elif operator in ("starts_with", "not_starts_with"):
        matched = property_string.startswith(filter_string)
    else:
        matched = property_string.endswith(filter_string)

    return not matched if operator.startswith("not_") else matched


def _match_regex_property(operator, value, override_value) -> bool:
    property_string = _value_to_string(
        override_value, UnresolvedFlagReason.MISSING_CONTEXT
    )
    regex_match = _match_regex(str(value), property_string)
    if regex_match is None:
        return False
    return not regex_match if operator == "not_regex" else regex_match


def _match_date_property(operator, value, override_value) -> bool:
    parsed_date = _parse_date_filter(value)

    if isinstance(override_value, datetime.datetime):
        override_date = convert_to_datetime_aware(override_value)
        return _compare_dates(operator, override_date, parsed_date)
    elif isinstance(override_value, datetime.date):
        return _compare_dates(operator, override_value, parsed_date.date())
    elif isinstance(override_value, str):
        try:
            override_date = parse_datetime(override_value)
            override_date = convert_to_datetime_aware(override_date)
            return _compare_dates(operator, override_date, parsed_date)
        except Exception:
            raise InconclusiveMatchError(
                "The date provided is not a valid format",
                reason=UnresolvedFlagReason.MISSING_CONTEXT,
            )
    else:
        raise InconclusiveMatchError(
            "The date provided must be a string or date object",
            reason=UnresolvedFlagReason.MISSING_CONTEXT,
        )


def _compare_dates(operator, override_date, parsed_date) -> bool:
    if operator == "is_date_before":
        return override_date < parsed_date
    return override_date > parsed_date


def _match_semver_property(operator, value, override_value) -> bool:
    flag_semver = _parse_semver_filter(operator, value)
    try:
        override_parsed = parse_semver(override_value)
    except (ValueError, TypeError):
        raise InconclusiveMatchError(
            f"Person property value '{override_value}' is not a valid semver",
            reason=UnresolvedFlagReason.MISSING_CONTEXT,
        )

    if operator in SEMVER_RANGE_OPERATORS:
        lower, upper = flag_semver
        return lower <= override_parsed < upper
    if operator == "semver_eq":
        return override_parsed == flag_semver
    if operator == "semver_neq":
        return override_parsed != flag_semver
    if operator == "semver_gt":
        return override_parsed > flag_semver
    if operator == "semver_gte":
        return override_parsed >= flag_semver
    if operator == "semver_lt":
        return override_parsed < flag_semver
    if operator == "semver_lte":
        return override_parsed <= flag_semver
    return _raise_unhandled_operator(operator)


def _match_scalar_property(operator, value, override_value) -> bool:
    if operator in _TEXT_OPERATORS:
        return _match_text_property(operator, value, override_value)
    if operator in ("regex", "not_regex"):
        return _match_regex_property(operator, value, override_value)
    if operator in DATE_OPERATORS:
        return _match_date_property(operator, value, override_value)
    if operator in SEMVER_OPERATORS:
        return _match_semver_property(operator, value, override_value)
    return _raise_unhandled_operator(operator)


def _raise_unhandled_operator(operator) -> NoReturn:
    # Unreachable: all operators in PROPERTY_OPERATORS are handled above,
    # and unknown operators are rejected at the top of _match_property_value.
    raise InconclusiveMatchError(
        f"Operator {operator} {_UNHANDLED_OPERATOR_MESSAGE}",
        reason=UnresolvedFlagReason.UNSUPPORTED_DEFINITION,
    )


_COHORT_OPERATORS = ("exact", "in", "not_in")


def match_cohort(
    property,
    property_values,
    cohort_properties,
    flags_by_key=None,
    evaluation_cache=None,
    distinct_id=None,
    device_id=None,
    property_matching_version: int = 1,
) -> bool:
    # Cohort properties are in the form of property groups like this:
    # {
    #     "cohort_id": {
    #         "type": "AND|OR",
    #         "values": [{
    #            "key": "property_name", "value": "property_value"
    #        }]
    #     }
    # }
    cohort_id = str(property.get("value"))
    if cohort_id not in cohort_properties:
        raise RequiresServerEvaluation(
            f"cohort {cohort_id} not found in local cohorts - likely a static cohort that requires server evaluation"
        )

    # Check the operator before the members, so an unsupported operator outranks a
    # member that is only missing context.
    operator = property.get("operator") or "exact"
    if operator not in _COHORT_OPERATORS:
        raise InconclusiveMatchError(
            f"Unsupported cohort operator: {operator}",
            reason=UnresolvedFlagReason.UNSUPPORTED_DEFINITION,
        )

    property_group = cohort_properties[cohort_id]
    matches = match_property_group(
        property_group,
        property_values,
        cohort_properties,
        flags_by_key,
        evaluation_cache,
        distinct_id,
        device_id=device_id,
        property_matching_version=property_matching_version,
    )
    return not matches if operator == "not_in" else matches


def _is_nested_property_group(prop) -> bool:
    """Whether a cohort group entry is itself a property group, not a filter."""
    return prop == {} or "values" in prop or prop.get("type") in ("AND", "OR")


def match_property_group(
    property_group,
    property_values,
    cohort_properties,
    flags_by_key=None,
    evaluation_cache=None,
    distinct_id=None,
    device_id=None,
    property_matching_version: int = 1,
) -> bool:
    is_and, properties = _get_property_group_values(property_group)
    if not properties:
        return True

    decisive_result = None
    inconclusive_reason: Optional[UnresolvedFlagReason] = None

    for prop in properties:
        try:
            effective_match = _match_property_group_entry(
                prop,
                property_values,
                cohort_properties,
                flags_by_key,
                evaluation_cache,
                distinct_id,
                device_id,
                property_matching_version,
            )
            if effective_match != is_and:
                decisive_result = effective_match
        except RequiresServerEvaluation:
            # Static/missing cohorts and malformed definitions always require server evaluation,
            # even when another branch would otherwise resolve the group locally.
            raise
        except InconclusiveMatchError as e:
            log.debug(f"Failed to compute property {prop} locally: {e}")
            inconclusive_reason = _pick_unresolved_reason(inconclusive_reason, e.reason)

    if decisive_result is not None:
        return decisive_result
    if inconclusive_reason is not None:
        raise InconclusiveMatchError(
            "Can't match cohort without a given cohort property value",
            reason=inconclusive_reason,
        )

    # AND: every entry matched. OR: none matched.
    return is_and


def _get_property_group_values(property_group):
    # The backend serializes its canonical empty PropertyGroup as {}.
    if property_group == {}:
        return True, []
    if not isinstance(property_group, dict):
        raise RequiresServerEvaluation("Cohort property group must be an object")

    property_group_type = property_group.get("type")
    if property_group_type not in ("AND", "OR"):
        raise RequiresServerEvaluation("Cohort property group type must be AND or OR")
    is_and = property_group_type == "AND"
    properties = property_group.get("values")
    if not isinstance(properties, list):
        raise RequiresServerEvaluation("Cohort property group values must be a list")
    return is_and, properties


def _match_property_group_entry(
    prop,
    property_values,
    cohort_properties,
    flags_by_key,
    evaluation_cache,
    distinct_id,
    device_id,
    property_matching_version,
):
    if not isinstance(prop, dict):
        raise RequiresServerEvaluation("Cohort property group entry must be an object")
    if _is_nested_property_group(prop):
        matches = match_property_group(
            prop,
            property_values,
            cohort_properties,
            flags_by_key,
            evaluation_cache,
            distinct_id,
            device_id=device_id,
            property_matching_version=property_matching_version,
        )
        negation = False
    else:
        matches = _match_condition_filter(
            prop,
            property_values,
            cohort_properties,
            flags_by_key,
            evaluation_cache,
            distinct_id,
            device_id=device_id,
            property_matching_version=property_matching_version,
        )
        negation = prop.get("negation", False)
    return matches != bool(negation)


def _get_definition_unresolved_reason(
    flag, flags_by_key, cohort_properties, group_type_mapping
) -> Optional[UnresolvedFlagReason]:
    """Return the unresolved reason that comes from the flag's definition alone, so
    it is known when definitions load. Matches the reason local evaluation reports
    for the same cause."""
    if not flag.get("active"):
        return None
    if flag.get("ensure_experience_continuity"):
        return UnresolvedFlagReason.EXPERIENCE_CONTINUITY
    flag_filters = flag.get("filters") or {}
    aggregation = flag_filters.get("aggregation_group_type_index")
    if aggregation is not None and not group_type_mapping.get(str(aggregation)):
        return UnresolvedFlagReason.UNSUPPORTED_DEFINITION
    if _has_unsupported_condition(flag_filters, flags_by_key, cohort_properties):
        return UnresolvedFlagReason.UNSUPPORTED_DEFINITION
    return None


def _has_unsupported_condition(flag_filters, flags_by_key, cohort_properties) -> bool:
    for condition in flag_filters.get("groups") or []:
        for prop in condition.get("properties") or []:
            if _is_unsupported_property(prop, flags_by_key, cohort_properties, set()):
                return True
    return False


def _is_unsupported_property(
    prop, flags_by_key, cohort_properties, visited_cohort_ids
) -> bool:
    property_type = prop.get("type")
    if property_type == "flag":
        return _is_unsupported_dependency(prop, flags_by_key)
    if property_type != "cohort":
        try:
            _assert_filter_supported(prop)
        except InconclusiveMatchError:
            return True
        return False
    cohort_id = str(prop.get("value"))
    if cohort_id not in cohort_properties:
        return True
    if (prop.get("operator") or "exact") not in _COHORT_OPERATORS:
        return True
    if cohort_id in visited_cohort_ids:
        return False
    visited_cohort_ids.add(cohort_id)
    return _is_unsupported_property_group(
        cohort_properties[cohort_id],
        flags_by_key,
        cohort_properties,
        visited_cohort_ids,
    )


def _is_unsupported_dependency(prop, flags_by_key) -> bool:
    # A malformed condition shape is a definitive local no-match, not a gap.
    if _flag_dependency_shape_problem(prop) is not None:
        return False
    dependency_chain = prop.get("dependency_chain")
    if not isinstance(dependency_chain, list) or not dependency_chain:
        return True
    return any(not flags_by_key.get(key) for key in dependency_chain)


def _is_unsupported_property_group(
    property_group, flags_by_key, cohort_properties, visited_cohort_ids
) -> bool:
    if property_group == {}:
        return False
    if not isinstance(property_group, dict):
        return True
    values = property_group.get("values")
    if property_group.get("type") not in ("AND", "OR") or not isinstance(values, list):
        return True
    for prop in values:
        if not isinstance(prop, dict):
            return True
        if _is_nested_property_group(prop):
            unsupported = _is_unsupported_property_group(
                prop, flags_by_key, cohort_properties, visited_cohort_ids
            )
        else:
            unsupported = _is_unsupported_property(
                prop, flags_by_key, cohort_properties, visited_cohort_ids
            )
        if unsupported:
            return True
    return False


def parse_datetime(value: str) -> datetime.datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    elif text.upper().endswith(" UTC"):
        text = text[:-4] + "+00:00"
    elif re.fullmatch(r"\d{4}", text):
        now = datetime.datetime.now()
        return datetime.datetime(int(text), now.month, now.day)

    text = re.sub(r" ([+-]\d{2}:\d{2})$", r"\1", text)
    return datetime.datetime.fromisoformat(text)


# Python's stdlib doesn't provide a calendar-aware month/year delta.
# Subtract one unit at a time and clamp the day after every step, so March 31
# minus two months is January 28 (through February 28), as the SDK spec requires.
def _subtract_months(dt: datetime.datetime, months: int) -> Optional[datetime.datetime]:
    current_index = dt.year * 12 + dt.month - 1
    month_index = current_index - months
    year = month_index // 12
    month = month_index % 12 + 1
    if not 1 <= year <= 9999:
        return None
    day = dt.day
    for index in range(current_index - 1, month_index - 1, -1):
        if day <= 28:  # Every month has at least 28 days, so nothing clamps further.
            break
        day = min(day, calendar.monthrange(index // 12, index % 12 + 1)[1])
    return dt.replace(year=year, month=month, day=day)


def _subtract_years(dt: datetime.datetime, years: int) -> Optional[datetime.datetime]:
    year = dt.year - years
    if not 1 <= year <= 9999:
        return None
    # The year before a leap year is never a leap year, so February 29 clamps on
    # the first step and stays on the 28th.
    day = 28 if years > 0 and (dt.month, dt.day) == (2, 29) else dt.day
    return dt.replace(year=year, day=day)


def relative_date_parse_for_feature_flag_matching(
    value: str,
) -> Optional[datetime.datetime]:
    regex = r"^-?(?P<number>[0-9]+)(?P<interval>[a-z])$"
    match = re.search(regex, value)
    parsed_dt = datetime.datetime.now(datetime.timezone.utc)
    if match:
        number = int(match.group("number"))

        if number >= 10_000:
            # Guard against overflow, disallow numbers greater than 10_000
            return None

        interval = match.group("interval")
        if interval == "h":
            parsed_dt = parsed_dt - datetime.timedelta(hours=number)
        elif interval == "d":
            parsed_dt = parsed_dt - datetime.timedelta(days=number)
        elif interval == "w":
            parsed_dt = parsed_dt - datetime.timedelta(weeks=number)
        elif interval == "m":
            return _subtract_months(parsed_dt, number)
        elif interval == "y":
            return _subtract_years(parsed_dt, number)
        else:
            return None

        return parsed_dt
    else:
        return None


def _semver_numeric_identifier(part: str) -> int:
    # Semver 2.0.0 §2: numeric identifiers MUST NOT include leading zeros.
    if not part or not part.isdigit():
        raise ValueError(f"Invalid semver numeric identifier: '{part}'")
    if len(part) > 1 and part[0] == "0":
        raise ValueError(f"Semver numeric identifier has leading zero: '{part}'")
    return int(part)


def parse_semver(value: str) -> tuple:
    """Parse a semver string into a comparable (major, minor, patch) integer tuple.

    Matches the behavior of the sortableSemver HogQL function:
    - Handles v-prefix, whitespace, pre-release suffixes
    - Defaults missing components to 0 (e.g., 1.2 -> 1.2.0)
    - Rejects numeric identifiers with leading zeros per semver 2.0.0 §2
    Raises ValueError if parsing fails.
    """
    text = str(value).strip().lstrip("vV")
    # Strip pre-release/build metadata suffix
    text = text.split("-")[0].split("+")[0]
    parts = text.split(".")

    if not parts or not parts[0]:
        raise ValueError("Invalid semver format")

    major = _semver_numeric_identifier(parts[0])
    minor = _semver_numeric_identifier(parts[1]) if len(parts) > 1 and parts[1] else 0
    patch = _semver_numeric_identifier(parts[2]) if len(parts) > 2 and parts[2] else 0

    return (major, minor, patch)


def _tilde_bounds(value: str) -> tuple:
    """~1.2.3 means >=1.2.3 <1.3.0 (allows patch-level changes)."""
    major, minor, patch = parse_semver(value)
    return (major, minor, patch), (major, minor + 1, 0)


def _caret_bounds(value: str) -> tuple:
    """Caret follows semver spec:
    ^1.2.3 means >=1.2.3 <2.0.0
    ^0.2.3 means >=0.2.3 <0.3.0
    ^0.0.3 means >=0.0.3 <0.0.4
    """
    major, minor, patch = parse_semver(value)
    lower = (major, minor, patch)

    if major > 0:
        upper = (major + 1, 0, 0)
    elif minor > 0:
        upper = (0, minor + 1, 0)
    else:
        upper = (0, 0, patch + 1)

    return lower, upper


def _wildcard_bounds(value: str) -> tuple:
    """Wildcard matching:
    1.* means >=1.0.0 <2.0.0
    1.2.* means >=1.2.0 <1.3.0
    """
    cleaned = str(value).strip().lstrip("vV").replace("*", "").rstrip(".")
    if not cleaned:
        raise ValueError("Invalid wildcard pattern")

    parts = [p for p in cleaned.split(".") if p]
    if not parts:
        raise ValueError("Invalid wildcard pattern")

    if len(parts) == 1:
        major = _semver_numeric_identifier(parts[0])
        return (major, 0, 0), (major + 1, 0, 0)
    elif len(parts) == 2:
        major, minor = (
            _semver_numeric_identifier(parts[0]),
            _semver_numeric_identifier(parts[1]),
        )
        return (major, minor, 0), (major, minor + 1, 0)
    else:
        major = _semver_numeric_identifier(parts[0])
        minor = _semver_numeric_identifier(parts[1])
        patch = _semver_numeric_identifier(parts[2])
        return (major, minor, patch), (major, minor, patch + 1)
