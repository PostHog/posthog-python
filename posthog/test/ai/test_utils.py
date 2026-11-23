"""Usage conversion fallback order and streaming merge ownership contracts."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from posthog.ai.types import TokenUsage
from posthog.ai.utils import merge_usage_stats, serialize_raw_usage


class PydanticUsage:
    def model_dump(self):
        return {"input_tokens": 4}

    def to_dict(self):
        raise AssertionError("model_dump must take precedence")


class ProtobufUsage:
    def model_dump(self):
        raise ValueError("unsupported model serialization")

    def to_dict(self):
        return {"output_tokens": 7}


class AttributeUsage:
    def __init__(self):
        self.input_tokens = 9

    def model_dump(self):
        raise ValueError("failed")

    def to_dict(self):
        raise ValueError("failed")


class StringUsage:
    __slots__ = ()
    model_dump = None
    to_dict = None

    def __str__(self):
        return "provider usage"


class UnreadableUsage(StringUsage):
    __slots__ = ()

    def __str__(self):
        raise ValueError("cannot stringify")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ({"input_tokens": 1}, {"input_tokens": 1}),
        (PydanticUsage(), {"input_tokens": 4}),
        (ProtobufUsage(), {"output_tokens": 7}),
        (AttributeUsage(), {"input_tokens": 9}),
        (SimpleNamespace(output_tokens=3), {"output_tokens": 3}),
        (StringUsage(), {"_raw": "provider usage"}),
        (UnreadableUsage(), None),
    ],
)
def test_serialize_raw_usage_follows_fallback_order_without_raising(raw, expected):
    assert serialize_raw_usage(raw) == expected


def test_serialize_raw_usage_preserves_already_serialized_mapping():
    raw = {"input_tokens": 0, "nested": {"details": 1}}
    assert serialize_raw_usage(raw) is raw


def test_merge_usage_stats_incremental_keeps_max_search_and_merges_raw_shallowly():
    target = TokenUsage(
        input_tokens=None,
        output_tokens=3,
        cache_read_input_tokens=2,
        cache_creation_input_tokens=4,
        reasoning_tokens=1,
        web_search_count=5,
        cache_reporting_exclusive=False,
        raw_usage={"input_tokens": 12, "details": {"old": 1}},
    )
    source = TokenUsage(
        input_tokens=10,
        output_tokens=0,
        cache_read_input_tokens=3,
        cache_creation_input_tokens=6,
        reasoning_tokens=2,
        web_search_count=2,
        cache_reporting_exclusive=True,
        raw_usage={"output_tokens": 7, "details": {"new": 2}},
    )
    original_source = deepcopy(source)
    previous_raw = target["raw_usage"]
    merge_usage_stats(target, source)
    assert target == {
        "input_tokens": 10,
        "output_tokens": 3,
        "cache_read_input_tokens": 5,
        "cache_creation_input_tokens": 10,
        "reasoning_tokens": 3,
        "web_search_count": 5,
        "cache_reporting_exclusive": True,
        "raw_usage": {"input_tokens": 12, "output_tokens": 7, "details": {"new": 2}},
    }
    assert previous_raw == {"input_tokens": 12, "details": {"old": 1}}
    assert source == original_source
    assert target["raw_usage"] is not source["raw_usage"]
    merge_usage_stats(
        target, TokenUsage(input_tokens=None, output_tokens=None, raw_usage=None)
    )
    assert target["input_tokens"] == 10
    assert target["output_tokens"] == 3
    assert target["raw_usage"] == {
        "input_tokens": 12,
        "output_tokens": 7,
        "details": {"new": 2},
    }


def test_merge_usage_stats_cumulative_replaces_zero_and_false_but_not_none():
    target = TokenUsage(
        input_tokens=10,
        output_tokens=8,
        cache_read_input_tokens=3,
        cache_creation_input_tokens=6,
        reasoning_tokens=4,
        web_search_count=7,
        cache_reporting_exclusive=True,
        raw_usage={"previous": 1},
    )
    source = TokenUsage(
        input_tokens=None,
        output_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        reasoning_tokens=0,
        web_search_count=0,
        cache_reporting_exclusive=False,
        raw_usage={"latest": 2},
    )
    merge_usage_stats(target, source, mode="cumulative")
    assert target == {
        "input_tokens": 10,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "reasoning_tokens": 0,
        "web_search_count": 0,
        "cache_reporting_exclusive": False,
        "raw_usage": {"latest": 2},
    }
    assert source["input_tokens"] is None
    assert target["raw_usage"] is source["raw_usage"]


def test_merge_usage_stats_invalid_mode_does_not_mutate_target():
    target = TokenUsage(input_tokens=10)
    with pytest.raises(ValueError, match="Invalid mode: unknown"):
        merge_usage_stats(target, TokenUsage(input_tokens=4), mode="unknown")
    assert target == {"input_tokens": 10}
