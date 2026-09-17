from unittest import mock

import pytest

from posthog.tracing import _limits as limits_module
from posthog.tracing._limits import (
    bound_attributes,
    truncate_attribute_value,
    truncate_attributes,
)
from posthog.tracing._otlp import (
    CIRCULAR_VALUE,
    MAX_VALUE_ITEMS,
    MAX_VALUE_NODES,
    TRUNCATED_VALUE,
    to_any_value,
)
from posthog.tracing._sanitize import FUNCTION_VALUE, UNSERIALIZABLE_VALUE


class TestTruncateAttributeValue:
    def test_truncates_a_long_string(self):
        assert truncate_attribute_value("x" * 40000, 8192) == "x" * 8192

    def test_never_walks_a_scalar(self):
        with mock.patch.object(limits_module, "_truncate") as walk:
            assert truncate_attribute_value("short", 8) == "short"
            assert truncate_attribute_value(7, 8) == 7
            assert truncate_attribute_value(None, 8) is None
        assert not walk.called

    def test_returns_a_short_string_unchanged(self):
        assert truncate_attribute_value("short", 8192) == "short"

    @pytest.mark.parametrize("value", [42, 1.5, True, None, 2**70])
    def test_leaves_numbers_booleans_and_none_alone(self, value):
        assert truncate_attribute_value(value, 3) == value

    def test_reaches_strings_nested_in_mappings_and_lists(self):
        value = {"body": "x" * 40000, "items": ["y" * 20, {"deep": "z" * 20}]}
        assert truncate_attribute_value(value, 8) == {
            "body": "x" * 8,
            "items": ["y" * 8, {"deep": "z" * 8}],
        }

    def test_does_not_mutate_the_callers_value(self):
        value = {"body": "x" * 20}
        truncate_attribute_value(value, 4)
        assert value == {"body": "x" * 20}

    def test_a_self_referencing_value_terminates_with_the_encoders_marker(self):
        value: dict = {"name": "n" * 20}
        value["self"] = value
        assert truncate_attribute_value(value, 4) == {
            "name": "nnnn",
            "self": CIRCULAR_VALUE,
        }

    def test_siblings_sharing_one_object_are_not_a_cycle(self):
        shared = {"k": "v" * 10}
        assert truncate_attribute_value([shared, shared], 2) == [
            {"k": "vv"},
            {"k": "vv"},
        ]

    def test_marks_items_past_the_encoders_item_cap(self):
        bounded = truncate_attribute_value(["a"] * (MAX_VALUE_ITEMS + 5), 8)
        assert len(bounded) == MAX_VALUE_ITEMS + 1
        assert bounded[-1] == TRUNCATED_VALUE
        # The encoder emits the same shape it would have for the original.
        assert to_any_value(bounded) == to_any_value(["a"] * (MAX_VALUE_ITEMS + 5))

    def test_stringifies_and_bounds_a_type_the_encoder_would_stringify(self):
        class Big:
            def __str__(self):
                return "b" * 100

        assert truncate_attribute_value(Big(), 10) == "b" * 10
        assert truncate_attribute_value(b"\x00" * 100, 10) == "b'\\x00\\x00"

    def test_a_key_the_encoder_skips_does_not_spend_the_walks_budget(self):
        # The encoder drops "" without charging for its value, so the walk must
        # too, or "x" would ship unbounded once the walk's budget ran out.
        value = {"": list(range(999)), "a": [list(range(999))] * 9, "x": "A" * 1000}
        bounded = truncate_attribute_value(value, 100)
        assert "" not in bounded
        assert bounded["x"] == "A" * 100
        encoded = to_any_value(bounded)["kvlistValue"]["values"]
        x = next(kv for kv in encoded if kv["key"] == "x")
        assert len(x["value"]["stringValue"]) == 100

    def test_leaves_a_callable_for_the_encoders_marker(self):
        def handler():
            pass

        assert truncate_attribute_value(handler, 3) is handler
        assert truncate_attribute_value({"fn": handler}, 3) == {"fn": handler}
        assert to_any_value(truncate_attribute_value(handler, 3)) == {
            "stringValue": FUNCTION_VALUE
        }

    def test_a_raising_str_costs_only_that_value(self):
        class Hostile:
            def __str__(self):
                raise RuntimeError("no")

        assert truncate_attribute_value({"a": Hostile(), "b": "ok"}, 8) == {
            "a": UNSERIALIZABLE_VALUE,
            "b": "ok",
        }

    def test_a_raising_accessor_costs_only_that_key(self):
        class Explosive(dict):
            def __getitem__(self, key):
                if key == "bad":
                    raise RuntimeError("no")
                return super().__getitem__(key)

        assert truncate_attribute_value(Explosive(good="g" * 9, bad=1), 3) == {
            "good": "ggg",
            "bad": UNSERIALIZABLE_VALUE,
        }


class TestBoundAttributes:
    def test_keeps_the_earliest_entries_and_counts_the_rest(self):
        source = {f"k{i}": i for i in range(130)}
        attributes, dropped = bound_attributes(source, 128, 8192)
        assert list(attributes) == [f"k{i}" for i in range(128)]
        assert dropped == 2

    @pytest.mark.parametrize(
        "source",
        [{"a": None, "b": 1, "c": 2}, {"b": 1, "c": 2, "a": None}],
        ids=["before-the-cap", "past-the-cap"],
    )
    def test_a_none_value_spends_no_slot_and_counts_no_drop(self, source):
        attributes, dropped = bound_attributes(source, 2, 8)
        assert attributes == {"b": 1, "c": 2}
        assert dropped == 0

    def test_a_real_value_past_the_cap_counts_a_drop(self):
        attributes, dropped = bound_attributes({"b": 1, "c": 2, "a": 3}, 2, 8)
        assert attributes == {"b": 1, "c": 2}
        assert dropped == 1

    def test_bounds_each_value(self):
        attributes, _ = bound_attributes({"a": "x" * 20}, 2, 5)
        assert attributes == {"a": "xxxxx"}

    def test_an_empty_key_spends_no_slot_and_counts_no_drop(self):
        attributes, dropped = bound_attributes({"": 1, "b": 2, "c": 3}, 2, 8)
        assert attributes == {"b": 2, "c": 3}
        assert dropped == 0


class TestTruncateAttributes:
    def test_bounds_every_value_as_a_copy(self):
        source = {"service.name": "api", "blob": "x" * 20}
        assert truncate_attributes(source, 4) == {"service.name": "api", "blob": "xxxx"}
        assert source["blob"] == "x" * 20


class TestWalkBounds:
    def test_walks_no_more_strings_than_the_encoder_would_emit(self):
        # A thousand paths to one shared list of a thousand strings. Leaves are
        # charged against the node budget, as in the encoder, so the walk does
        # not copy every string on every path.
        inner = ["x" * 50] * 1000
        bounded = truncate_attribute_value([inner] * 1000, 8)
        walked = sum(
            1
            for items in bounded
            if items is not inner
            for item in items
            if item == "x" * 8
        )
        assert 0 < walked <= MAX_VALUE_NODES

    def test_stops_walking_a_mapping_at_the_encoders_item_cap(self):
        value = {f"k{i}": "v" * 50 for i in range(MAX_VALUE_ITEMS + 50)}
        bounded = truncate_attribute_value(value, 4)
        assert len(bounded) == MAX_VALUE_ITEMS
