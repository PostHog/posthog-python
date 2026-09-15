"""Event-only normalization at sizing and wire serialization boundaries."""

from typing import Any


def _omit_null_members(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _omit_null_members(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, (list, tuple)):
        return [_omit_null_members(item) for item in value]
    return value


def _clean_event_properties(event: Any) -> Any:
    # Conversion of custom objects remains the responsibility of utils.clean.
    # Do not change envelopes, non-event requests, or caller-owned dictionaries.
    if not isinstance(event, dict):
        return event
    result = dict(event)
    properties = event.get("properties")
    if isinstance(properties, dict):
        cleaned = {}
        for key, value in properties.items():
            # Typed SDK exception metadata keeps its field-specific null rules.
            if key == "$exception_list" and event.get("event") == "$exception":
                cleaned[key] = value
            elif (
                value is None
                and event.get("event") == "$feature_flag_called"
                and (
                    key == "$feature_flag_response"
                    or (
                        isinstance(properties.get("$feature_flag"), str)
                        and key == f"$feature/{properties['$feature_flag']}"
                    )
                )
            ):
                # Missing/error flag evaluations intentionally emit null. Only
                # preserve the exact emitted flag key, not other custom $feature/ keys.
                cleaned[key] = value
            elif value is not None:
                cleaned[key] = _omit_null_members(value)
        result["properties"] = cleaned
    for key in ("$set", "$set_once"):
        if key in event:
            result[key] = _omit_null_members(event[key])
    return result
