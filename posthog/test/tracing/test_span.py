import asyncio
import contextvars
import gc
import sys
import threading
import time
import weakref
from contextvars import ContextVar
from datetime import datetime, timezone
from unittest import mock

import pytest

from posthog.tracing import _span as span_module
from posthog.tracing._config import MAX_ATTRIBUTES_PER_EVENT
from posthog.tracing._otlp import CIRCULAR_VALUE, SpanRecord, build_otlp_span
from posthog.tracing._sanitize import (
    FALLBACK_SPAN_NAME,
    FUNCTION_VALUE,
    UNSERIALIZABLE_VALUE,
)
from posthog.tracing._span import (
    NOOP_SPAN,
    ClockAnchor,
    NoopSpan,
    PassThroughSpan,
    RecordingSpan,
    Span,
    describe_error,
    inert_span,
)

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
START_NS = 1_700_000_000_000_000_000


def make_span(records=None, **overrides) -> RecordingSpan:
    records = records if records is not None else []
    init = dict(
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        name="checkout",
        start_ns=START_NS,
        backdated=True,
        on_end=records.append,
    )
    init.update(overrides)
    return RecordingSpan(**init)


class TestRecording:
    def test_produces_exactly_one_record_on_end(self):
        records: list = []
        span = make_span(records)
        span.end()
        assert len(records) == 1
        assert isinstance(records[0], SpanRecord)
        assert records[0].name == "checkout"
        assert records[0].trace_id == TRACE_ID
        assert records[0].span_id == SPAN_ID

    def test_is_idempotent_on_end(self):
        records: list = []
        span = make_span(records)
        span.end()
        span.end()
        assert len(records) == 1

    def test_ignores_operations_after_end(self):
        records: list = []
        span = make_span(records)
        span.end()
        span.set_attribute("k", "v").add_event("late").set_status("error").update_name(
            "other"
        )
        assert records[0].attributes == {}
        assert records[0].events == []
        assert records[0].status is None
        assert records[0].name == "checkout"

    def test_chains_mutators(self):
        span = make_span()
        assert (
            span.set_attribute("a", 1).set_attributes({"b": 2}).add_event("e") is span
        )

    def test_replaces_the_name_up_until_end(self):
        records: list = []
        span = make_span(records, name="HTTP request")
        span.update_name("GET /users/:id")
        span.end()
        assert records[0].name == "GET /users/:id"

    def test_replaces_an_empty_name_rather_than_dropping_the_span(self):
        records: list = []
        span = make_span(records)
        span.update_name("")
        span.end()
        assert records[0].name == FALLBACK_SPAN_NAME

    def test_applies_last_write_wins_to_status(self):
        records: list = []
        span = make_span(records)
        span.set_status("error", "boom").set_status("ok")
        span.end()
        assert records[0].status is not None
        assert records[0].status.code == "ok"
        assert records[0].status.message is None

    def test_omits_status_when_never_set(self):
        records: list = []
        make_span(records).end()
        assert records[0].status is None

    def test_ignores_a_status_code_whose_comparison_raises(self):
        class Hostile:
            def __eq__(self, other):
                raise RuntimeError("no")

            __hash__ = object.__hash__

        records: list = []
        span = make_span(records)
        span.set_status(Hostile())  # type: ignore[arg-type]
        span.end()
        assert records[0].status is None

    def test_ignores_an_unrecognized_status_along_with_its_message(self):
        records: list = []
        span = make_span(records)
        span.set_status("weird", "nope")
        span.end()
        assert records[0].status is None

    def test_never_raises_when_the_pipeline_callback_fails(self):
        def explode(record):
            raise RuntimeError("queue broken")

        make_span(on_end=explode).end()


class TestAttributes:
    def test_user_attributes_are_kept_on_the_record(self):
        records: list = []
        span = make_span(records)
        span.set_attribute("plan", "pro").set_attributes({"n": 1})
        span.end()
        assert records[0].attributes == {"plan": "pro", "n": 1}

    def test_marks_only_the_raising_key_on_set_attributes(self):
        class Explosive(dict):
            def __getitem__(self, key):
                if key == "bad":
                    raise RuntimeError("boom")
                return super().__getitem__(key)

        records: list = []
        span = make_span(records)
        span.set_attributes(Explosive(good=1, bad=2))
        span.end()
        assert records[0].attributes == {"good": 1, "bad": UNSERIALIZABLE_VALUE}

    def test_snapshots_event_attributes_so_a_reused_mapping_cannot_mutate_them(self):
        records: list = []
        span = make_span(records)
        shared = {"step": 1}
        span.add_event("a", shared)
        shared["step"] = 2
        span.add_event("b", shared)
        span.end()
        assert records[0].events[0].attributes == {"step": 1}
        assert records[0].events[1].attributes == {"step": 2}

    def test_stringifies_a_non_string_attribute_key(self):
        records: list = []
        span = make_span(records)
        span.set_attribute(7, "x")  # type: ignore[arg-type]
        span.end()
        assert records[0].attributes == {"7": "x"}

    def test_ignores_a_key_that_cannot_be_stringified(self):
        class HostileKey:
            def __str__(self):
                raise RuntimeError("no")

        records: list = []
        span = make_span(records)
        span.set_attribute(HostileKey(), "x")  # type: ignore[arg-type]
        span.end()
        assert records[0].attributes == {}


class TestRecordException:
    def test_sets_error_status_and_attaches_an_event_without_ending(self):
        records: list = []
        span = make_span(records)
        span.record_exception(TypeError("boom"))
        assert records == []
        span.end()
        assert records[0].status is not None
        assert records[0].status.code == "error"
        assert records[0].status.message == "boom"
        assert records[0].events[0].name == "exception"
        assert records[0].events[0].attributes == {
            "exception.type": "TypeError",
            "exception.message": "boom",
        }

    def test_overrides_an_explicit_ok(self):
        records: list = []
        span = make_span(records)
        span.set_status("ok").record_exception(ValueError("x"))
        span.end()
        assert records[0].status is not None
        assert records[0].status.code == "error"


class TestMonotonicClock:
    def test_measures_duration_against_the_monotonic_reading_not_the_wall_clock(self):
        records: list = []
        with mock.patch.object(
            span_module.time, "monotonic_ns", side_effect=[10, 10 + 80_000_000]
        ):
            span = make_span(records, backdated=False)
            span.end()
        assert records[0].end_ns - records[0].start_ns == 80_000_000

    def test_never_reports_a_negative_duration_when_the_monotonic_source_goes_backwards(
        self,
    ):
        records: list = []
        with mock.patch.object(span_module.time, "monotonic_ns", side_effect=[100, 50]):
            span = make_span(records, backdated=False)
            span.end()
        assert records[0].end_ns == records[0].start_ns

    def test_places_an_event_inside_the_span_window(self):
        records: list = []
        with mock.patch.object(
            span_module.time, "monotonic_ns", side_effect=[0, 30_000_000, 80_000_000]
        ):
            span = make_span(records, backdated=False)
            span.add_event("cache miss")
            span.end()
        event_ns = records[0].events[0].timestamp_ns
        assert records[0].start_ns <= event_ns <= records[0].end_ns
        assert event_ns == START_NS + 30_000_000

    def test_uses_the_wall_clock_for_a_backdated_span(self):
        records: list = []
        with mock.patch.object(span_module.time, "time_ns", return_value=START_NS + 5):
            span = make_span(records, backdated=True)
            span.end()
        assert records[0].end_ns == START_NS + 5


class TestTimestamps:
    def test_records_an_end_at_or_after_the_start(self):
        records: list = []
        make_span(records, backdated=False).end()
        assert records[0].end_ns >= records[0].start_ns

    def test_honours_an_explicit_end_time(self):
        records: list = []
        make_span(records).end(1_700_000_001)
        assert records[0].end_ns == START_NS + 10**9

    def test_accepts_a_datetime_as_an_end_time(self):
        records: list = []
        make_span(records).end(datetime(2023, 11, 14, 22, 13, 21, tzinfo=timezone.utc))
        assert records[0].end_ns == START_NS + 10**9

    def test_corrects_an_end_before_the_start_to_a_zero_duration(self):
        records: list = []
        make_span(records).end(1_699_000_000)
        assert records[0].end_ns == START_NS

    def test_falls_back_to_the_derived_end_for_an_out_of_range_end_time(self):
        records: list = []
        with mock.patch.object(span_module.time, "time_ns", return_value=START_NS + 7):
            make_span(records).end(-5)
        assert records[0].end_ns == START_NS + 7

    def test_honours_an_explicit_event_timestamp(self):
        records: list = []
        span = make_span(records)
        span.add_event("e", timestamp=1_700_000_000.5)
        span.end()
        assert records[0].events[0].timestamp_ns == START_NS + 500_000_000


class TestContextPropagation:
    def test_produces_a_sampled_traceparent(self):
        assert make_span().traceparent() == f"00-{TRACE_ID}-{SPAN_ID}-01"

    def test_returns_none_tracestate_when_it_has_none(self):
        assert make_span().tracestate() is None

    def test_returns_the_tracestate_it_was_created_with(self):
        assert make_span(trace_state="vendor=abc").tracestate() == "vendor=abc"

    def test_propagates_the_trace_flags_it_was_started_with(self):
        assert (
            make_span(trace_flags="00").traceparent() == f"00-{TRACE_ID}-{SPAN_ID}-00"
        )

    def test_hands_a_child_the_flags_it_propagates(self):
        context = make_span(trace_flags="00", trace_state="v=1")._child_context()
        assert (context.trace_id, context.parent_span_id) == (TRACE_ID, SPAN_ID)
        assert (context.trace_state, context.trace_flags) == ("v=1", "00")


class TestClockAnchor:
    def test_a_root_anchors_its_children_to_its_own_start(self):
        with mock.patch.object(span_module.time, "monotonic_ns", return_value=500):
            span = make_span(backdated=False)
        assert span._child_context().clock_anchor == ClockAnchor(START_NS, 500)

    def test_a_backdated_span_hands_its_children_no_anchor(self):
        assert make_span(backdated=True)._child_context().clock_anchor is None

    def test_a_child_starts_on_its_anchor_and_passes_the_same_anchor_on(self):
        anchor = ClockAnchor(START_NS, 1_000)
        with mock.patch.object(span_module.time, "monotonic_ns", return_value=1_250):
            child = make_span(backdated=False, clock_anchor=anchor, start_ns=1)
        assert child._start_ns == START_NS + 250
        assert child._child_context().clock_anchor == anchor

    def test_a_backdated_child_keeps_its_own_start(self):
        child = make_span(backdated=True, clock_anchor=ClockAnchor(START_NS, 1_000))
        assert child._start_ns == START_NS

    def test_records_parent_and_remoteness(self):
        records: list = []
        make_span(
            records, parent_span_id="b7ad6b7169203331", parent_is_remote=True
        ).end()
        assert records[0].parent_span_id == "b7ad6b7169203331"
        assert records[0].parent_is_remote is True


class TestContextManager:
    def test_activates_for_the_block_and_ends_on_exit(self):
        active: ContextVar = ContextVar("active", default=None)
        records: list = []
        span = make_span(records, active_var=active)
        assert active.get() is None
        with span as entered:
            assert entered is span
            assert active.get() is span
        assert active.get() is None
        assert len(records) == 1

    def test_records_a_raised_exception_and_reraises_it_unchanged(self):
        records: list = []
        error = TypeError("boom")
        with pytest.raises(TypeError) as raised:
            with make_span(records):
                raise error
        assert raised.value is error
        assert records[0].status is not None
        assert records[0].status.code == "error"
        assert records[0].status.message == "boom"
        attributes = records[0].events[0].attributes
        assert attributes["exception.type"] == "TypeError"
        assert attributes["exception.message"] == "boom"
        assert attributes["exception.stacktrace"].startswith(
            "Traceback (most recent call last):"
        )
        assert "TypeError: boom" in attributes["exception.stacktrace"]

    @pytest.mark.parametrize(
        "error", [GeneratorExit(), asyncio.CancelledError(), KeyboardInterrupt()]
    )
    def test_ends_without_recording_a_base_exception_that_is_control_flow(self, error):
        records: list = []
        with pytest.raises(type(error)):
            with make_span(records):
                raise error
        assert len(records) == 1
        assert records[0].status is None
        assert records[0].events == []

    def test_a_closed_generator_holding_a_span_is_not_an_error(self):
        records: list = []

        def stream():
            with make_span(records):
                yield 1
                yield 2

        consumer = stream()
        next(consumer)
        consumer.close()
        assert len(records) == 1
        assert records[0].status is None

    def test_treats_an_explicit_ok_status_as_final_when_the_block_raises(self):
        records: list = []
        with pytest.raises(ValueError):
            with make_span(records) as span:
                span.set_status("ok")
                raise ValueError("x")
        assert records[0].status is not None
        assert records[0].status.code == "ok"
        assert records[0].events[0].name == "exception"

    def test_deactivates_even_when_the_block_raises(self):
        active: ContextVar = ContextVar("active", default=None)
        with pytest.raises(RuntimeError):
            with make_span(active_var=active):
                raise RuntimeError("x")
        assert active.get() is None

    def test_is_no_longer_active_when_on_end_runs(self):
        active: ContextVar = ContextVar("active", default=None)
        seen: list = []
        span = make_span(active_var=active, on_end=lambda _: seen.append(active.get()))
        with span:
            pass
        assert seen == [None]

    def test_exiting_in_a_context_that_never_entered_leaves_it_active(self, caplog):
        active: ContextVar = ContextVar("active", default=None)
        span = make_span(active_var=active)
        span.__enter__()
        with caplog.at_level("DEBUG", logger="posthog"):
            contextvars.copy_context().run(span.__exit__, None, None, None)
        assert active.get() is span
        assert "never entered it" in caplog.text
        span.__exit__(None, None, None)
        assert active.get() is None

    def test_ending_inside_the_block_does_not_double_record(self):
        records: list = []
        with make_span(records) as span:
            span.end()
        assert len(records) == 1

    def test_nested_blocks_restore_the_outer_span(self):
        active: ContextVar = ContextVar("active", default=None)
        outer = make_span(active_var=active)
        inner = make_span(active_var=active)
        with outer:
            with inner:
                assert active.get() is inner
            assert active.get() is outer
        assert active.get() is None


class TestActivationAcrossContexts:
    def test_one_span_entered_in_two_threads_leaves_neither_active(self):
        active: ContextVar = ContextVar("active", default=None)
        span = make_span(active_var=active)
        entered, exited = (
            threading.Barrier(2, timeout=5),
            threading.Barrier(2, timeout=5),
        )
        seen = {}

        def worker(name, exit_first):
            span.__enter__()
            entered.wait()
            if not exit_first:
                exited.wait()
            span._deactivate()
            if exit_first:
                exited.wait()
            seen[name] = active.get()

        threads = [
            threading.Thread(target=worker, args=("a", True)),
            threading.Thread(target=worker, args=("b", False)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert seen == {"a": None, "b": None}

    def test_one_span_entered_in_two_tasks_leaves_neither_active(self):
        active: ContextVar = ContextVar("active", default=None)
        span = make_span(active_var=active)

        async def task(first_in, first_out):
            await first_in.wait()
            with span:
                first_out.set()
                await asyncio.sleep(0.01)
            return active.get()

        async def main():
            go, a_in = asyncio.Event(), asyncio.Event()
            go.set()
            return await asyncio.gather(task(go, a_in), task(a_in, asyncio.Event()))

        assert asyncio.run(main()) == [None, None]

    def test_one_span_entered_concurrently_in_many_threads_stays_consistent(self):
        active: ContextVar = ContextVar("active", default=None)
        span = make_span(active_var=active)
        errors: list = []
        seen = {}

        def worker(name):
            try:
                for _ in range(2000):
                    with span:
                        pass
            except Exception as e:
                errors.append(e)
            seen[name] = active.get()

        interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        finally:
            sys.setswitchinterval(interval)
        assert errors == []
        assert span._tokens == []
        assert seen == {i: None for i in range(8)}


class TestNoopSpan:
    def test_supports_the_full_surface_without_raising(self):
        span = NoopSpan()
        assert span.set_attribute("k", "v") is span
        assert span.set_attributes({"k": "v"}) is span
        assert span.add_event("e", {"k": "v"}, 1) is span
        assert span.set_status("error", "m") is span
        assert span.record_exception(ValueError()) is span
        assert span.update_name("n") is span
        span.end()
        span.end(1)
        with span as entered:
            assert entered is span

    def test_never_produces_a_traceparent(self):
        assert NOOP_SPAN.traceparent() is None
        assert NOOP_SPAN.tracestate() is None

    def test_is_never_activated(self):
        active: ContextVar = ContextVar("active", default=None)
        with inert_span(active_var=active) as span:
            assert span is NOOP_SPAN
            assert active.get() is None

    def test_all_handles_are_spans(self):
        assert isinstance(NOOP_SPAN, Span)
        assert isinstance(make_span(), Span)
        assert isinstance(
            PassThroughSpan("00-" + TRACE_ID + "-" + SPAN_ID + "-01"), Span
        )


class TestInertSpan:
    def test_returns_the_shared_noop_without_a_usable_parent(self):
        assert inert_span() is NOOP_SPAN
        assert inert_span("garbage") is NOOP_SPAN
        assert inert_span(NOOP_SPAN) is NOOP_SPAN

    def test_echoes_an_inbound_traceparent_flags_and_version_included(self):
        span = inert_span(f"01-{TRACE_ID}-{SPAN_ID}-00", "vendor=abc")
        assert isinstance(span, PassThroughSpan)
        assert span.traceparent() == f"01-{TRACE_ID}-{SPAN_ID}-00"
        assert span.tracestate() == "vendor=abc"

    def test_discards_an_invalid_tracestate_without_losing_the_traceparent(self):
        span = inert_span(f"00-{TRACE_ID}-{SPAN_ID}-01", "novalue")
        assert span.traceparent() == f"00-{TRACE_ID}-{SPAN_ID}-01"
        assert span.tracestate() is None

    def test_pass_through_is_activated_by_the_scoped_form(self):
        active: ContextVar = ContextVar("active", default=None)
        span = inert_span(f"00-{TRACE_ID}-{SPAN_ID}-01", active_var=active)
        with span:
            assert active.get() is span
        assert active.get() is None

    def test_a_blank_parent_echoes_the_active_pass_through(self):
        active: ContextVar = ContextVar("active", default=None)
        with inert_span(f"00-{TRACE_ID}-{SPAN_ID}-01", active_var=active):
            span = inert_span("  ", active_var=active)
        assert span.traceparent() == f"00-{TRACE_ID}-{SPAN_ID}-01"

    def test_echoes_the_active_pass_through_when_no_parent_is_given(self):
        # Tracing off, a span nested inside the one that received the inbound
        # trace: it must keep forwarding that trace, not return a no-op.
        active: ContextVar = ContextVar("active", default=None)
        outer = inert_span(f"00-{TRACE_ID}-{SPAN_ID}-00", "vendor=abc", active)
        with outer:
            inner = inert_span(active_var=active)
            assert isinstance(inner, PassThroughSpan)
            assert inner.traceparent() == f"00-{TRACE_ID}-{SPAN_ID}-00"
            assert inner.tracestate() == "vendor=abc"
            with inner:
                assert active.get() is inner
            assert active.get() is outer

    def test_echoes_an_explicit_handle_parent(self):
        outer = inert_span(f"00-{TRACE_ID}-{SPAN_ID}-01", "vendor=abc")
        child = inert_span(outer)
        assert child.traceparent() == f"00-{TRACE_ID}-{SPAN_ID}-01"
        assert child.tracestate() == "vendor=abc"

    def test_an_explicit_parent_wins_over_the_active_span(self):
        active: ContextVar = ContextVar("active", default=None)
        with inert_span(f"00-{TRACE_ID}-{SPAN_ID}-01", active_var=active):
            assert inert_span(NOOP_SPAN, active_var=active) is NOOP_SPAN

    def test_unwraps_a_one_element_header_list(self):
        span = inert_span([f"00-{TRACE_ID}-{SPAN_ID}-01"])
        assert span.traceparent() == f"00-{TRACE_ID}-{SPAN_ID}-01"

    def test_nothing_active_and_no_parent_is_a_noop(self):
        active: ContextVar = ContextVar("active", default=None)
        assert inert_span(active_var=active) is NOOP_SPAN

    def test_a_parent_whose_traceparent_raises_gives_a_noop(self):
        class Hostile(Span):
            def traceparent(self):
                raise RuntimeError("no")

        assert inert_span(Hostile()) is NOOP_SPAN


class TestDescribeError:
    @pytest.mark.parametrize(
        "error,expected",
        [
            (TypeError("boom"), ("TypeError", "boom")),
            (ValueError(), ("ValueError", "")),
            (KeyboardInterrupt(), ("KeyboardInterrupt", "")),
            ("plain string", ("str", "plain string")),
            (42, ("int", "42")),
        ],
    )
    def test_describes_values(self, error, expected):
        assert describe_error(error) == expected

    def test_survives_a_value_whose_str_raises(self):
        class Hostile(Exception):
            def __str__(self):
                raise RuntimeError("no")

        assert describe_error(Hostile()) == ("Hostile", "")


class TestHandleLifetime:
    def test_a_dropped_handle_is_collectable(self):
        span = make_span()
        ref = weakref.ref(span)
        del span
        gc.collect()
        assert ref() is None


class TestSpanLimits:
    def test_keeps_the_first_attributes_and_counts_the_overflow(self):
        records: list = []
        span = make_span(records, max_attributes=128)
        for i in range(130):
            span.set_attribute(f"k{i}", i)
        span.end()
        assert len(records[0].attributes) == 128
        assert "k127" in records[0].attributes and "k128" not in records[0].attributes
        assert records[0].dropped_attributes_count == 2

    def test_overwriting_a_key_does_not_spend_a_slot(self):
        records: list = []
        span = make_span(records, max_attributes=1)
        span.set_attribute("a", 1).set_attribute("a", 2).set_attribute("b", 3)
        span.end()
        assert records[0].attributes == {"a": 2}
        assert records[0].dropped_attributes_count == 1

    def test_none_removes_a_key_and_frees_its_slot(self):
        records: list = []
        span = make_span(records, max_attributes=1)
        span.set_attribute("a", 1).set_attribute("a", None).set_attribute("b", 2)
        span.end()
        assert records[0].attributes == {"b": 2}
        assert records[0].dropped_attributes_count == 0

    def test_auto_context_is_exempt_from_the_cap_and_never_evicted(self):
        records: list = []
        span = make_span(
            records,
            attributes={"posthogDistinctId": "user-1", "sessionId": "s-1"},
            auto_attribute_keys=["posthogDistinctId", "sessionId"],
            max_attributes=2,
        )
        span.set_attributes({"a": 1, "b": 2, "c": 3})
        span.end()
        assert records[0].attributes == {
            "posthogDistinctId": "user-1",
            "sessionId": "s-1",
            "a": 1,
            "b": 2,
        }
        assert records[0].dropped_attributes_count == 1

    def test_keeps_the_first_events_and_counts_the_overflow(self):
        records: list = []
        span = make_span(records, max_events=2)
        span.add_event("a").add_event("b").add_event("c")
        span.end()
        assert [e.name for e in records[0].events] == ["a", "b"]
        assert records[0].dropped_events_count == 1

    def test_a_recorded_exception_spends_an_event_slot(self):
        records: list = []
        span = make_span(records, max_events=1)
        span.add_event("a")
        span.record_exception(ValueError("boom"))
        span.end()
        assert [e.name for e in records[0].events] == ["a"]
        assert records[0].dropped_events_count == 1
        assert records[0].status.code == "error"

    def test_caps_each_events_attributes(self):
        records: list = []
        span = make_span(records)
        span.add_event("batch", {f"k{i}": i for i in range(130)})
        span.end()
        event = records[0].events[0]
        assert len(event.attributes) == 128
        assert event.dropped_attributes_count == 2

    def test_truncates_a_long_attribute_value_without_counting_a_drop(self):
        records: list = []
        span = make_span(records, max_attribute_value_length=8192)
        span.set_attribute("payload", "x" * 40000)
        span.set_attribute("nested", {"body": "y" * 40000})
        span.end()
        assert records[0].attributes["payload"] == "x" * 8192
        assert records[0].attributes["nested"] == {"body": "y" * 8192}
        assert records[0].dropped_attributes_count == 0

    def test_a_callable_reaches_the_encoder_as_its_marker(self):
        def handler():
            pass

        records: list = []
        span = make_span(records)
        span.set_attribute("fn", handler)
        span.add_event("e", {"fn": handler, "nested": {"fn": handler}})
        span.end()
        encoded = build_otlp_span(records[0])
        assert encoded["attributes"] == [
            {"key": "fn", "value": {"stringValue": FUNCTION_VALUE}}
        ]
        assert encoded["events"][0]["attributes"] == [
            {"key": "fn", "value": {"stringValue": FUNCTION_VALUE}},
            {
                "key": "nested",
                "value": {
                    "kvlistValue": {
                        "values": [
                            {"key": "fn", "value": {"stringValue": FUNCTION_VALUE}}
                        ]
                    }
                },
            },
        ]

    def test_a_none_event_attribute_past_the_cap_counts_no_drop(self):
        records: list = []
        span = make_span(records)
        attributes = {f"k{i}": i for i in range(MAX_ATTRIBUTES_PER_EVENT)}
        attributes["late"] = None
        span.add_event("batch", attributes)
        span.end()
        event = records[0].events[0]
        assert len(event.attributes) == MAX_ATTRIBUTES_PER_EVENT
        assert event.dropped_attributes_count == 0

    def test_truncates_event_attributes_names_and_status_messages(self):
        records: list = []
        span = make_span(records, max_attribute_value_length=4)
        span.add_event("long event name", {"k": "long value"})
        span.update_name("long span name")
        span.set_status("error", "long message")
        span.end()
        record = records[0]
        assert record.name == "long"
        assert record.events[0].name == "long"
        assert record.events[0].attributes == {"k": "long"}
        assert record.status.message == "long"

    def test_a_self_referencing_attribute_is_stored_as_the_encoders_marker(self):
        records: list = []
        value: dict = {}
        value["self"] = value
        span = make_span(records)
        span.set_attribute("loop", value)
        span.end()
        assert records[0].attributes["loop"] == {"self": CIRCULAR_VALUE}

    def test_an_empty_key_spends_no_slot(self):
        records: list = []
        span = make_span(records, max_attributes=1)
        span.set_attribute("", 1).set_attribute("real", 2)
        span.set_attributes({"": 3})
        span.add_event("e", {"": 1, "k": 2})
        span.end()
        assert records[0].attributes == {"real": 2}
        assert records[0].dropped_attributes_count == 0
        assert records[0].events[0].attributes == {"k": 2}

    def test_add_event_with_a_non_mapping_logs_and_keeps_the_event(self, caplog):
        records: list = []
        span = make_span(records)
        with caplog.at_level("DEBUG", logger="posthog"):
            span.add_event("e", ["not", "a", "mapping"])
        span.end()
        assert records[0].events[0].attributes == {}
        assert "expected a mapping" in caplog.text


class TestConcurrentWrites:
    def _run(self, worker, count=8):
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

    def test_writes_to_one_key_from_many_threads_spend_one_slot(self):
        records: list = []
        span = make_span(records, max_attributes=1)

        def worker(i):
            for n in range(300):
                span.set_attribute("k", n)

        self._run(worker)
        span.end()
        assert list(records[0].attributes) == ["k"]
        assert records[0].dropped_attributes_count == 0

    def test_events_from_many_threads_never_exceed_the_cap(self):
        records: list = []
        span = make_span(records, max_events=100)

        def worker(i):
            for n in range(50):
                span.add_event(f"{i}-{n}")

        self._run(worker)
        span.end()
        assert len(records[0].events) == 100
        assert records[0].dropped_events_count == 300

    def test_a_write_racing_end_never_lands_after_the_record(self):
        records: list = []
        span = make_span(records)
        stop = threading.Event()

        def writer(i):
            n = 0
            while not stop.is_set():
                span.set_attribute("k", n)
                span.add_event("e")
                n += 1

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        time.sleep(0.02)
        span.end()
        stop.set()
        for thread in threads:
            thread.join(30)
        record = records[0]
        assert len(records) == 1
        assert span._events == record.events
        assert {k: v for k, v in span._attributes.items() if v is not None} == (
            record.attributes
        )


class TestExceptionStacktrace:
    def test_record_exception_attaches_the_stack_of_a_raised_exception(self):
        records: list = []
        span = make_span(records)
        try:
            raise KeyError("missing")
        except KeyError as e:
            span.record_exception(e)
        span.end()
        stack = records[0].events[0].attributes["exception.stacktrace"]
        assert stack.startswith("Traceback (most recent call last):")
        assert "KeyError: 'missing'" in stack

    def test_omits_the_stack_of_an_exception_that_was_never_raised(self):
        records: list = []
        span = make_span(records)
        span.record_exception(ValueError("constructed, not raised"))
        span.end()
        assert "exception.stacktrace" not in records[0].events[0].attributes

    def test_bounds_the_stack_keeping_the_outermost_exception(self):
        # Python prints a cause first and the outermost exception last, so
        # the tail keeps the outermost raise and may cut the cause off.
        def recurse(depth):
            if depth == 0:
                raise ValueError("the actual crash site")
            recurse(depth - 1)

        records: list = []
        span = make_span(records, max_attribute_value_length=300)
        try:
            try:
                recurse(60)
            except ValueError as cause:
                raise RuntimeError("wrapped") from cause
        except RuntimeError as e:
            span.record_exception(e)
        span.end()
        stack = records[0].events[0].attributes["exception.stacktrace"]
        assert len(stack) == 300
        assert stack.rstrip().endswith("RuntimeError: wrapped")

    def test_a_stack_that_cannot_be_formatted_costs_only_the_attribute(self):
        records: list = []
        span = make_span(records)
        try:
            raise ValueError("boom")
        except ValueError as e:
            with mock.patch.object(
                span_module.traceback,
                "format_exception",
                side_effect=RuntimeError("no"),
            ):
                span.record_exception(e)
        span.end()
        attributes = records[0].events[0].attributes
        assert attributes["exception.message"] == "boom"
        assert "exception.stacktrace" not in attributes


class TestSpanLimitInteractions:
    def test_the_attribute_and_event_caps_are_independent(self):
        records: list = []
        span = make_span(records, max_attributes=1, max_events=1)
        span.set_attributes({"a": 1, "b": 2})
        span.add_event("e1", {"x": 1, "y": 2})
        span.add_event("e2")
        span.end()
        record = records[0]
        assert record.attributes == {"a": 1}
        assert record.events[0].attributes == {"x": 1, "y": 2}
        assert (record.dropped_attributes_count, record.dropped_events_count) == (1, 1)

    def test_a_raising_key_in_add_event_costs_only_that_key(self):
        class HostileKey:
            def __str__(self):
                raise RuntimeError("no")

        records: list = []
        span = make_span(records)
        span.add_event("e", {HostileKey(): 1, "ok": 2})
        span.end()
        assert records[0].events[0].attributes == {"ok": 2}

    def test_the_per_event_attribute_cap_is_opentelemetrys_default(self):
        assert MAX_ATTRIBUTES_PER_EVENT == 128
