import math
from datetime import datetime, timezone

import pytest

from posthog.tracing._sanitize import (
    FALLBACK_SPAN_NAME,
    MAX_TIMESTAMP_NS,
    UNSERIALIZABLE_VALUE,
    clamp_end_ns,
    copy_user_attributes,
    resolve_start_ns,
    resolve_supplied_ns,
    sanitize_name,
    to_epoch_ns,
)

NOW_NS = 1_700_000_000_000_000_000


class TestSanitizeName:
    def test_keeps_a_non_empty_string(self):
        assert sanitize_name("checkout", "Span name") == "checkout"

    @pytest.mark.parametrize("name", ["", "   ", None, 42, ["a"]])
    def test_replaces_an_unusable_name(self, name):
        assert sanitize_name(name, "Span name") == FALLBACK_SPAN_NAME


class TestToEpochNs:
    def test_converts_an_aware_datetime(self):
        dt = datetime(2023, 11, 14, 22, 13, 20, 40_000, tzinfo=timezone.utc)
        assert to_epoch_ns(dt) == NOW_NS + 40_000_000

    def test_treats_a_naive_datetime_as_local_time(self):
        naive = datetime(2023, 11, 14, 22, 13, 20)
        assert to_epoch_ns(naive) == int(naive.timestamp()) * 10**9

    def test_converts_integer_seconds(self):
        assert to_epoch_ns(1_700_000_000) == NOW_NS

    def test_converts_float_seconds(self):
        assert to_epoch_ns(1_700_000_000.5) == NOW_NS + 500_000_000

    @pytest.mark.parametrize(
        "value",
        [
            None,
            True,
            "1700000000",
            float("nan"),
            float("inf"),
            -1,
            MAX_TIMESTAMP_NS // 10**9 + 1,
            datetime(1960, 1, 1, tzinfo=timezone.utc),
        ],
    )
    def test_rejects_unusable_values(self, value):
        assert to_epoch_ns(value) is None

    def test_rejects_a_datetime_whose_arithmetic_raises(self):
        class Hostile(datetime):
            def __sub__(self, other):
                raise RuntimeError("no")

        assert to_epoch_ns(Hostile(2023, 1, 1, tzinfo=timezone.utc)) is None


class TestResolveStartNs:
    def test_uses_now_when_nothing_is_supplied(self):
        assert resolve_start_ns(None, NOW_NS) == NOW_NS

    def test_uses_now_for_an_unusable_value(self):
        assert resolve_start_ns("yesterday", NOW_NS) == NOW_NS

    def test_backdates_to_a_supplied_time(self):
        assert resolve_start_ns(1_699_999_000, NOW_NS) == 1_699_999_000 * 10**9

    def test_warns_when_the_server_will_clamp(self, caplog):
        caplog.set_level("DEBUG", logger="posthog")
        resolve_start_ns(1_700_000_000 - 48 * 3600, NOW_NS)
        assert any("24 hours" in r.getMessage() for r in caplog.records)

    def test_keeps_a_future_start_and_warns(self, caplog):
        caplog.set_level("DEBUG", logger="posthog")
        future = 1_700_000_000 + 3600
        assert resolve_start_ns(future, NOW_NS) == future * 10**9
        assert "in the future" in caplog.text


class TestEndAndSuppliedTimes:
    def test_clamps_an_end_before_the_start(self):
        assert clamp_end_ns(NOW_NS - 1, NOW_NS) == NOW_NS

    def test_keeps_an_end_after_the_start(self):
        assert clamp_end_ns(NOW_NS + 5, NOW_NS) == NOW_NS + 5

    def test_supplied_time_wins_when_valid(self):
        assert resolve_supplied_ns(1_700_000_001, NOW_NS, "end time") == NOW_NS + 10**9

    @pytest.mark.parametrize("value", [None, "soon", math.nan, -5])
    def test_falls_back_to_the_derived_time(self, value):
        assert resolve_supplied_ns(value, NOW_NS, "end time") == NOW_NS


class TestCopyUserAttributes:
    def test_copies_a_mapping(self):
        assert copy_user_attributes({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_user_keys_win_on_collision(self):
        assert copy_user_attributes({"a": 1}, {"a": 2}) == {"a": 2}

    def test_ignores_none_and_non_mappings(self):
        assert copy_user_attributes({"a": 1}, None) == {"a": 1}
        assert copy_user_attributes({"a": 1}, ["b"]) == {"a": 1}

    def test_marks_only_the_raising_key(self):
        class Explosive(dict):
            def __getitem__(self, key):
                if key == "bad":
                    raise RuntimeError("boom")
                return super().__getitem__(key)

        source = Explosive(good=1, bad=2)
        assert copy_user_attributes({}, source) == {
            "good": 1,
            "bad": UNSERIALIZABLE_VALUE,
        }

    def test_stringifies_non_string_keys(self):
        assert copy_user_attributes({}, {1: "x"}) == {"1": "x"}

    def test_drops_only_a_key_that_cannot_be_stringified(self):
        class HostileKey:
            def __str__(self):
                raise RuntimeError("no")

            def __hash__(self):
                return 1

        assert copy_user_attributes({}, {HostileKey(): 1, "ok": 2}) == {"ok": 2}
