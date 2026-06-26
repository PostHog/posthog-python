"""Unit tests for the layered truncation pipeline (_truncation.py).

This module bounds untrusted/large tool payloads before capture, so its branches
(string caps, depth/breadth limits, cycle detection, the 100KB size budget) are
worth exercising directly rather than only through the end-to-end paths.
"""

from datetime import datetime, timezone

from posthog.mcp._truncation import (
    MAX_EVENT_BYTES,
    MAX_STRING_LENGTH,
    _json_byte_size,
    normalize,
    truncate_event,
)

# --- normalize ---------------------------------------------------------------


def test_normalize_caps_long_strings():
    out = normalize("x" * (MAX_STRING_LENGTH + 100))
    assert out.endswith("...") and len(out) == MAX_STRING_LENGTH + 3


def test_normalize_keeps_bool_distinct_from_int():
    # bool is an int subclass — it must be handled before the int branch.
    assert normalize(True) is True
    assert normalize(False) is False
    assert normalize(7) == 7


def test_normalize_handles_nan_and_infinity():
    assert normalize(float("nan")) == "[NaN]"
    assert normalize(float("inf")) == "[Infinity]"
    assert normalize(float("-inf")) == "[-Infinity]"


def test_normalize_datetime_to_isoformat():
    dt = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert normalize(dt) == dt.isoformat()


def test_normalize_callable_becomes_placeholder():
    def my_tool():
        pass

    assert normalize(my_tool) == "[Function: my_tool]"


def test_normalize_bounds_depth():
    nested: dict = {}
    cur = nested
    for _ in range(15):  # deeper than MAX_DEPTH (10)
        cur["x"] = {}
        cur = cur["x"]
    import json

    assert "[Object]" in json.dumps(normalize(nested))


def test_normalize_bounds_dict_breadth():
    out = normalize({f"k{i}": i for i in range(150)})
    assert out["..."] == "[MaxProperties ~]"
    assert len(out) == 101  # 100 kept + the marker


def test_normalize_bounds_array_breadth():
    out = normalize(list(range(150)))
    assert out[-1] == "[MaxProperties ~]"
    assert len(out) == 101


def test_normalize_detects_dict_cycle():
    d: dict = {}
    d["self"] = d
    assert normalize(d)["self"] == "[Circular ~]"


def test_normalize_detects_list_cycle():
    a: list = []
    a.append(a)
    assert normalize(a)[0] == "[Circular ~]"


def test_normalize_coerces_unknown_objects_to_str():
    class Widget:
        def __str__(self):
            return "widget-instance"

    assert normalize(Widget()) == "widget-instance"


# --- field-level truncation --------------------------------------------------


def test_truncate_event_caps_metadata_fields():
    out = truncate_event({"user_intent": "i" * 5000, "resource_name": "r" * 500})
    assert len(out["user_intent"]) == 2048 + 3
    assert len(out["resource_name"]) == 256 + 3


def test_truncate_event_caps_exception_value_and_frames():
    frames = [{"frame": i} for i in range(120)]
    out = truncate_event(
        {
            "error": {
                "$exception_list": [
                    {"value": "e" * 5000, "stacktrace": {"frames": frames}}
                ]
            }
        }
    )
    exc = out["error"]["$exception_list"][0]
    assert len(exc["value"]) == 2048 + 3
    assert len(exc["stacktrace"]["frames"]) == 50  # head + tail


def test_truncate_event_caps_response_text_block():
    out = truncate_event(
        {"response": {"content": [{"type": "text", "text": "t" * 40000}]}}
    )
    assert len(out["response"]["content"][0]["text"]) == 32_768 + 3


# --- size-targeted budget ----------------------------------------------------


def test_truncate_event_enforces_byte_budget_with_many_strings():
    # Each string is under the 32KB per-string cap, but together they blow the
    # 100KB event budget — exercising the largest-field trimming loop.
    event = {
        "event_type": "$mcp_tool_call",
        "parameters": {f"field_{i}": "z" * 5000 for i in range(60)},
    }
    assert _json_byte_size(event) > MAX_EVENT_BYTES
    out = truncate_event(event)
    assert _json_byte_size(out) <= MAX_EVENT_BYTES


def test_truncate_event_caps_single_huge_string_under_budget():
    out = truncate_event({"parameters": {"blob": "z" * 300_000}})
    assert _json_byte_size(out) <= MAX_EVENT_BYTES


def test_truncate_event_leaves_small_events_untouched():
    event = {"event_type": "$mcp_tool_call", "resource_name": "echo", "is_error": False}
    assert truncate_event(event) == event
