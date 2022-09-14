import datetime
import hashlib
import re

from dateutil import parser

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
    hash_value = _hash(flag["key"], distinct_id, salt="variant")
    for variant in variant_lookup_table(flag):
        if hash_value >= variant["value_min"] and hash_value < variant["value_max"]:
            return variant["key"]
    return None


def variant_lookup_table(feature_flag):
    lookup_table = []
    value_min = 0
    multivariates = ((feature_flag.get("filters") or {}).get("multivariate") or {}).get("variants") or []
    for variant in multivariates:
        value_max = value_min + variant["rollout_percentage"] / 100
        lookup_table.append({"value_min": value_min, "value_max": value_max, "key": variant["key"]})
        value_min = value_max
    return lookup_table


def match_feature_flag_properties(flag, distinct_id, properties):
    flag_conditions = (flag.get("filters") or {}).get("groups") or []
    is_inconclusive = False

    for condition in flag_conditions:
        try:
            # if any one condition resolves to True, we can shortcircuit and return
            # the matching variant
            if is_condition_match(flag, distinct_id, condition, properties):
                return get_matching_variant(flag, distinct_id) or True
        except InconclusiveMatchError:
            is_inconclusive = True

    if is_inconclusive:
        raise InconclusiveMatchError("Can't determine if feature flag is enabled or not with given properties")

    # We can only return False when either all conditions are False, or
    # no condition was inconclusive.
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

    if key not in property_values:
        raise InconclusiveMatchError("can't match properties without a given property value")

    if operator == "is_not_set":
        raise InconclusiveMatchError("can't match properties with operator is_not_set")

    override_value = property_values[key]

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

    if operator in ["is_date_before", "is_date_after"]:
        try:
            parsed_date = parser.parse(value)
        except Exception:
            raise InconclusiveMatchError("The date set on the flag is not a valid format")

        if isinstance(override_value, datetime.date):
            if operator == "is_date_before":
                return override_value < parsed_date
            else:
                return override_value > parsed_date
        elif isinstance(override_value, str):
            try:
                override_date = parser.parse(override_value)
                if operator == "is_date_before":
                    return override_date < parsed_date
                else:
                    return override_date > parsed_date
            except Exception:
                raise InconclusiveMatchError("The date provided is not a valid format")
        else:
            raise InconclusiveMatchError("The date provided must be a string or date object")

    return False
