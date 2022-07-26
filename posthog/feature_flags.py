import hashlib
import re

from posthog.utils import is_valid_regex

__LONG_SCALE__ = float(0xFFFFFFFFFFFFFFF)


class InconclusiveMatchError(Exception):
    pass


# This function takes a distinct_id and a feature flag key and returns a float between 0 and 1.
# Given the same distinct_id and key, it'll always return the same float. These floats are
# uniformly distributed between 0 and 1, so if we want to show this feature to 20% of traffic
# we can do _hash(key, distinct_id) < 0.2
def _hash(key, distinct_id, salt=""):
    hash_key = f"{key}.{distinct_id}{salt}"
    hash_val = int(hashlib.sha1(hash_key.encode("utf-8")).hexdigest()[:15], 16)
    return hash_val / __LONG_SCALE__


def get_matching_variant(flag, distinct_id):
    for variant in variant_lookup_table(flag):
        if (
            _hash(flag["key"], distinct_id, salt="variant") >= variant["value_min"]
            and _hash(flag["key"], distinct_id, salt="variant") < variant["value_max"]
        ):
            return variant["key"]
    return None


def variant_lookup_table(feature_flag):
    lookup_table = []
    value_min = 0
    # TODO: convert to `or {}`
    multivariates = (feature_flag.get("filters", {}).get("multivariate") or {}).get("variants") or []
    for variant in multivariates:
        value_max = value_min + variant["rollout_percentage"] / 100
        lookup_table.append({"value_min": value_min, "value_max": value_max, "key": variant["key"]})
        value_min = value_max
    return lookup_table


def match_feature_flag_properties(flag, distinct_id, properties):
    # TODO: convert to `or {}`
    flag_conditions = flag.get("filters", {}).get("groups") or []
    is_match = any(is_condition_match(flag, distinct_id, condition, properties) for condition in flag_conditions)
    if is_match:
        return get_matching_variant(flag, distinct_id) or True
    else:
        return False


def is_condition_match(feature_flag, distinct_id, condition, properties):
    rollout_percentage = condition.get("rollout_percentage")
    if len(condition.get("properties") or []) > 0:
        if not all(match_property(prop, properties) for prop in condition.get("properties")):
            return False
        elif rollout_percentage is None:
            return True

    if rollout_percentage is not None and _hash(feature_flag["key"], distinct_id) > (rollout_percentage / 100):
        return False

    return True


def match_property(property, property_values) -> bool:
    # only looks for matches where key exists in override_property_values
    # doesn't support operator is_not_set
    key = property.get("key")
    operator = property.get("operator") or "exact"
    value = property.get("value")
    override_value = property_values[key]

    if key not in property_values:
        raise InconclusiveMatchError("can't match properties without a given property value")

    if operator == "is_not_set":
        raise InconclusiveMatchError("can't match properties with operator is_not_set")

    if operator == "exact":
        if isinstance(value, list):
            return override_value in value
        return value == override_value

    if operator == "is_not":
        if isinstance(value, list):
            return override_value not in value
        return value != override_value

    if operator == "is_set":
        return key in property_values

    if operator == "icontains":
        return str(value).lower() in str(override_value).lower()

    if operator == "not_icontains":
        return str(value).lower() not in str(override_value).lower()

    if operator == "regex":
        return is_valid_regex(str(value)) and re.compile(str(value)).search(str(override_value)) is not None

    if operator == "not_regex":
        return is_valid_regex(str(value)) and re.compile(str(value)).search(str(override_value)) is None

    if operator == "gt":
        return type(override_value) == type(value) and override_value > value

    if operator == "gte":
        return type(override_value) == type(value) and override_value >= value

    if operator == "lt":
        return type(override_value) == type(value) and override_value < value

    if operator == "lte":
        return type(override_value) == type(value) and override_value <= value

    return False
