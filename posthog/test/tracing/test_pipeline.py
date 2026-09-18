import gc
import logging
import threading
import time
import weakref
from types import SimpleNamespace
from unittest import mock

import pytest

from posthog.test.tracing.helpers import (
    SPAN_ID,
    TRACE_ID,
    FakeSender,
    clock,
    fake_timers,
    make,
    make_traces,
    queued,
)
from posthog.tracing import _pipeline as pipeline_module
from posthog.tracing import _span as span_module
from posthog.tracing._drops import DropLog
from posthog.tracing._transport import SendOutcome
from posthog.tracing._span import NOOP_SPAN, PassThroughSpan, RecordingSpan

__all__ = ["clock", "fake_timers"]


class TestStartSpan:
    def test_enqueues_exactly_one_record_per_span(self):
        pipeline, _, _ = make()
        pipeline.start_span("a").end()
        assert len(queued(pipeline)) == 1

    def test_gives_a_root_span_a_fresh_trace_id_and_no_parent(self):
        pipeline, _, _ = make()
        pipeline.start_span("a").end()
        pipeline.start_span("b").end()
        a, b = queued(pipeline)
        assert len(a.trace_id) == 32 and len(a.span_id) == 16
        assert a.parent_span_id is None
        assert a.trace_id != b.trace_id

    def test_does_not_activate_the_span_it_returns(self):
        pipeline, _, active = make()
        span = pipeline.start_span("a")
        assert active.get() is None
        span.end()

    def test_parents_a_child_to_an_explicit_span_handle(self):
        pipeline, _, _ = make()
        parent = pipeline.start_span("parent")
        child = pipeline.start_span("child", parent=parent)
        child.end()
        parent.end()
        child_record, parent_record = queued(pipeline)
        assert child_record.trace_id == parent_record.trace_id
        assert child_record.parent_span_id == parent_record.span_id
        assert child_record.parent_is_remote is False

    def test_defaults_kind_to_internal_and_honours_an_explicit_kind(self):
        pipeline, _, _ = make()
        pipeline.start_span("a").end()
        pipeline.start_span("b", kind="server").end()
        assert [r.kind for r in queued(pipeline)] == ["internal", "server"]

    def test_returns_an_inert_handle_when_the_client_is_disabled(self):
        pipeline, _, _ = make(client=SimpleNamespace(disabled=True, send=True))
        span = pipeline.start_span("a")
        assert span is NOOP_SPAN
        span.end()
        assert queued(pipeline) == []

    def test_makes_a_child_of_an_inert_handle_inert_rather_than_an_orphan(self):
        pipeline, _, _ = make()
        child = pipeline.start_span("child", parent=NOOP_SPAN)
        assert child is NOOP_SPAN
        child = pipeline.start_span(
            "child", parent=PassThroughSpan(f"00-{TRACE_ID}-{SPAN_ID}-01", "v=1")
        )
        # Inert like its parent, but still carrying the inbound context onward.
        assert isinstance(child, PassThroughSpan)
        assert child.traceparent() == f"00-{TRACE_ID}-{SPAN_ID}-01"
        assert child.tracestate() == "v=1"
        child.end()
        assert queued(pipeline) == []

    def test_never_raises_out_of_start_span(self):
        pipeline, _, _ = make()
        with mock.patch.object(
            pipeline, "_start_span", side_effect=RuntimeError("boom")
        ):
            span = pipeline.start_span("a")
        assert span is NOOP_SPAN


class TestStartTime:
    def test_backdates_the_span_to_a_supplied_start(self):
        pipeline, _, _ = make()
        pipeline.start_span("a", start_time=1_700_000_000).end()
        assert queued(pipeline)[0].start_ns == 1_700_000_000 * 10**9

    def test_falls_back_to_now_for_an_unusable_start(self):
        pipeline, _, _ = make()
        with mock.patch.object(time, "time_ns", return_value=42 * 10**9):
            pipeline.start_span("a", start_time="yesterday").end()
        assert queued(pipeline)[0].start_ns == 42 * 10**9


class TestClockBasis:
    """Children of a local parent share its root's clock, so they stay inside it."""

    @pytest.fixture
    def clocks(self):
        state = {"wall": 0, "mono": 0}
        with (
            mock.patch.object(time, "time_ns", lambda: state["wall"]),
            mock.patch.object(span_module.time, "time_ns", lambda: state["wall"]),
            mock.patch.object(span_module.time, "monotonic_ns", lambda: state["mono"]),
        ):
            yield lambda wall_ms, mono_ms: state.update(
                wall=int(wall_ms * 1_000_000), mono=int(mono_ms * 1_000_000)
            )

    @staticmethod
    def assert_within(inner, outer):
        assert inner.start_ns >= outer.start_ns
        assert inner.end_ns <= outer.end_ns

    def test_keeps_a_child_inside_its_parent_across_sub_millisecond_starts(
        self, clocks
    ):
        pipeline, _, _ = make()
        clocks(1_700_000_001_000, 0.9)
        parent = pipeline.start_span("POST /checkout")
        clocks(1_700_000_001_001, 1)
        child = pipeline.start_span("http.post payments", parent=parent)
        clocks(1_700_000_001_005, 5)
        child.end()
        clocks(1_700_000_001_005, 5.05)
        parent.end()
        child_record, parent_record = queued(pipeline)
        self.assert_within(child_record, parent_record)

    def test_keeps_nested_active_spans_inside_their_parents_across_a_clock_step(
        self, clocks
    ):
        pipeline, _, _ = make()
        clocks(1_700_000_001_000, 100)
        with pipeline.start_span("root"):
            clocks(1_700_000_000_510, 110)
            with pipeline.start_span("child"):
                clocks(1_700_000_000_515, 115)
                with pipeline.start_span("grandchild"):
                    clocks(1_700_000_000_520, 120)
                clocks(1_700_000_000_525, 125)
            clocks(1_700_000_000_530, 130)
        grandchild, child, root = queued(pipeline)
        self.assert_within(child, root)
        self.assert_within(grandchild, child)

    def test_keeps_its_own_clock_under_a_remote_or_backdated_parent_or_backdated(
        self, clocks
    ):
        pipeline, _, _ = make()
        clocks(1_700_000_001_000, 100)
        parent = pipeline.start_span("parent")
        backdated_parent = pipeline.start_span(
            "backdated parent", start_time=1_700_000_000
        )
        clocks(1_700_000_000_500, 110)
        pipeline.start_span("remote child", parent=f"00-{TRACE_ID}-{SPAN_ID}-01").end()
        pipeline.start_span("child of backdated", parent=backdated_parent).end()
        pipeline.start_span(
            "backdated child", parent=parent, start_time=1_700_000_000.2
        ).end()
        assert [r.start_ns for r in queued(pipeline)] == [
            1_700_000_000_500_000_000,
            1_700_000_000_500_000_000,
            1_700_000_000_200_000_000,
        ]

    def test_keeps_an_explicit_start_time_that_equals_the_current_time(self, clocks):
        pipeline, _, _ = make()
        clocks(1_700_000_001_000, 100)
        parent = pipeline.start_span("parent")
        clocks(1_700_000_000_500, 110)
        pipeline.start_span("child", parent=parent, start_time=1_700_000_000.5).end()
        assert queued(pipeline)[0].start_ns == 1_700_000_000_500_000_000


class TestTraceContinuation:
    def test_continues_a_remote_trace_from_a_traceparent_string(self):
        pipeline, _, _ = make()
        pipeline.start_span("a", parent=f"00-{TRACE_ID}-{SPAN_ID}-01").end()
        record = queued(pipeline)[0]
        assert record.trace_id == TRACE_ID
        assert record.parent_span_id == SPAN_ID
        assert record.parent_is_remote is True
        assert record.trace_flags == "01"

    def test_continues_a_trace_the_caller_sampled_out_and_propagates_the_flag(self):
        pipeline, _, _ = make()
        span = pipeline.start_span("a", parent=f"00-{TRACE_ID}-{SPAN_ID}-00")
        assert span.traceparent().endswith("-00")
        span.end()
        assert queued(pipeline)[0].trace_flags == "00"

    def test_preserves_tracestate_opaquely_and_passes_it_to_children(self):
        pipeline, _, _ = make()
        parent = pipeline.start_span(
            "a", parent=f"00-{TRACE_ID}-{SPAN_ID}-01", tracestate="vendor=abc"
        )
        child = pipeline.start_span("b", parent=parent, tracestate="ignored=1")
        assert child.tracestate() == "vendor=abc"
        child.end()
        parent.end()
        assert [r.trace_state for r in queued(pipeline)] == ["vendor=abc", "vendor=abc"]

    def test_starts_a_fresh_root_on_a_malformed_traceparent_without_raising(self):
        pipeline, _, _ = make()
        pipeline.start_span("a", parent="garbage").end()
        record = queued(pipeline)[0]
        assert record.parent_span_id is None
        assert record.trace_id != TRACE_ID

    @pytest.mark.parametrize("blank", ["", "   ", [""]])
    def test_a_blank_parent_means_the_default_parent(self, blank):
        pipeline, _, _ = make()
        with pipeline.start_span("outer") as outer:
            pipeline.start_span("inner", parent=blank).end()
        assert queued(pipeline)[0].parent_span_id == outer._span_id

    @pytest.mark.parametrize("unusable", ["garbage", ["dup", "header"], object()])
    def test_an_unusable_explicit_parent_starts_a_new_trace(self, unusable):
        # The same fallback as a malformed header: never the active span, which
        # would silently attach the span to a trace the caller did not name.
        pipeline, _, _ = make()
        with pipeline.start_span("outer") as outer:
            pipeline.start_span("inner", parent=unusable).end()
        record = queued(pipeline)[0]
        assert record.parent_span_id is None
        assert record.trace_id != outer._trace_id

    def test_a_local_child_of_a_sampled_out_trace_keeps_the_00_flag(self):
        pipeline, _, _ = make()
        parent = pipeline.start_span("a", parent=f"00-{TRACE_ID}-{SPAN_ID}-00")
        child = pipeline.start_span("b", parent=parent)
        assert child.traceparent().endswith("-00")
        child.end()
        parent.end()
        assert [r.trace_flags for r in queued(pipeline)] == ["00", "00"]
        assert [r.parent_is_remote for r in queued(pipeline)] == [False, True]

    def test_two_header_values_with_nothing_active_start_a_fresh_root(self):
        pipeline, _, _ = make()
        headers = [f"00-{TRACE_ID}-{SPAN_ID}-01", f"00-{TRACE_ID}-{SPAN_ID}-00"]
        pipeline.start_span("a", parent=headers).end()
        record = queued(pipeline)[0]
        assert record.parent_span_id is None and record.trace_id != TRACE_ID

    def test_a_parent_whose_traceparent_raises_gives_an_inert_span(self):
        class Hostile(NOOP_SPAN.__class__):
            def traceparent(self):
                raise RuntimeError("no")

        pipeline, _, _ = make()
        assert pipeline.start_span("a", parent=Hostile()) is NOOP_SPAN

    def test_continues_a_trace_from_a_raw_asgi_header_value(self):
        pipeline, _, _ = make()
        pipeline.start_span("a", parent=f"00-{TRACE_ID}-{SPAN_ID}-01".encode()).end()
        assert queued(pipeline)[0].trace_id == TRACE_ID
        assert queued(pipeline)[0].parent_span_id == SPAN_ID

    def test_continues_a_trace_from_a_one_element_header_list(self):
        pipeline, _, _ = make()
        pipeline.start_span("a", parent=[f"00-{TRACE_ID}-{SPAN_ID}-01"]).end()
        assert queued(pipeline)[0].trace_id == TRACE_ID
        assert queued(pipeline)[0].parent_span_id == SPAN_ID

    def test_an_active_pass_through_parents_a_recorded_span_as_remote(self):
        # A pass-through is active when an earlier span in this trace could not
        # be recorded; the inbound trace must survive it, not restart.
        pipeline, _, active = make()
        pass_through = PassThroughSpan(
            f"00-{TRACE_ID}-{SPAN_ID}-00", "vendor=abc", active
        )
        with pass_through:
            pipeline.start_span("child").end()
        record = queued(pipeline)[0]
        assert record.trace_id == TRACE_ID
        assert record.parent_span_id == SPAN_ID
        assert record.parent_is_remote is True
        assert record.trace_flags == "00"
        assert record.trace_state == "vendor=abc"


class TestActiveSpan:
    def test_nests_spans_started_inside_a_with_block(self):
        pipeline, _, active = make()
        with pipeline.start_span("parent") as parent:
            assert active.get() is parent
            pipeline.start_span("child").end()
        assert active.get() is None
        child, parent_record = queued(pipeline)
        assert child.parent_span_id == parent_record.span_id
        assert child.trace_id == parent_record.trace_id

    def test_lets_an_explicit_parent_override_the_active_span(self):
        pipeline, _, _ = make()
        other = pipeline.start_span("other")
        with pipeline.start_span("active"):
            pipeline.start_span("child", parent=other).end()
        assert queued(pipeline)[0].parent_span_id == other._span_id
        other.end()

    def test_records_a_raised_error_and_reraises_it_unmodified(self):
        pipeline, _, _ = make()
        error = ValueError("boom")
        with pytest.raises(ValueError) as raised:
            with pipeline.start_span("job"):
                raise error
        assert raised.value is error
        record = queued(pipeline)[0]
        assert record.status.code == "error"
        assert record.events[0].attributes["exception.type"] == "ValueError"

    def test_isolates_concurrent_threads_from_each_other(self):
        pipeline, _, active = make()
        seen = {}
        barrier = threading.Barrier(2, timeout=5)

        def work(name):
            with pipeline.start_span(name) as span:
                barrier.wait()
                seen[name] = active.get() is span

        threads = [threading.Thread(target=work, args=(n,)) for n in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert seen == {"a": True, "b": True}


class TestPassThroughWhenTracingCannotRun:
    def test_echoes_an_inbound_traceparent_flags_included_when_disabled(self):
        pipeline, _, _ = make(client=SimpleNamespace(disabled=True, send=True))
        span = pipeline.start_span(
            "a", parent=f"01-{TRACE_ID}-{SPAN_ID}-00", tracestate="v=1"
        )
        assert span.traceparent() == f"01-{TRACE_ID}-{SPAN_ID}-00"
        assert span.tracestate() == "v=1"
        span.end()
        assert queued(pipeline) == []

    def test_activates_the_pass_through_handle_so_it_can_propagate(self):
        pipeline, _, active = make(client=SimpleNamespace(disabled=True, send=True))
        with pipeline.start_span("a", parent=f"00-{TRACE_ID}-{SPAN_ID}-01") as span:
            assert active.get() is span
        assert active.get() is None

    def test_a_blank_parent_still_forwards_the_active_pass_through(self):
        pipeline, _, _ = make(client=SimpleNamespace(disabled=True, send=True))
        with pipeline.start_span("a", parent=f"00-{TRACE_ID}-{SPAN_ID}-01"):
            child = pipeline.start_span("b", parent="")
        assert child.traceparent() == f"00-{TRACE_ID}-{SPAN_ID}-01"

    def test_passes_the_inbound_context_through_when_the_live_span_limit_refuses(self):
        pipeline, _, _ = make(max_live_spans=1)
        held = pipeline.start_span("held")
        span = pipeline.start_span("refused", parent=f"00-{TRACE_ID}-{SPAN_ID}-01")
        assert span.traceparent() == f"00-{TRACE_ID}-{SPAN_ID}-01"
        held.end()


class TestAutoContext:
    def test_attaches_the_distinct_id_and_session_id_as_join_keys(self):
        pipeline, _, _ = make(context={"distinct_id": "user-1", "session_id": "sess-1"})
        pipeline.start_span("a").end()
        assert queued(pipeline)[0].attributes == {
            "posthogDistinctId": "user-1",
            "sessionId": "sess-1",
        }

    def test_omits_keys_with_no_value(self):
        pipeline, _, _ = make(context={"distinct_id": "", "session_id": None})
        pipeline.start_span("a").end()
        assert queued(pipeline)[0].attributes == {}

    def test_stringifies_ids_and_keeps_a_zero(self):
        pipeline, _, _ = make(context={"distinct_id": 0, "session_id": 42})
        pipeline.start_span("a").end()
        assert queued(pipeline)[0].attributes == {
            "posthogDistinctId": "0",
            "sessionId": "42",
        }

    def test_still_records_the_span_when_the_context_is_not_a_mapping(self):
        pipeline, _, _ = make()
        pipeline._get_context = lambda: "not a mapping"
        pipeline.start_span("a").end()
        assert queued(pipeline)[0].attributes == {}

    def test_freezes_the_snapshot_at_span_start(self):
        context = {"distinct_id": "a"}
        pipeline, _, _ = make(context=context)
        span = pipeline.start_span("a")
        context["distinct_id"] = "b"
        span.end()
        assert queued(pipeline)[0].attributes["posthogDistinctId"] == "a"

    def test_lets_user_attributes_win_on_collision(self):
        pipeline, _, _ = make(context={"distinct_id": "a"})
        pipeline.start_span("a", attributes={"posthogDistinctId": "override"}).end()
        assert queued(pipeline)[0].attributes["posthogDistinctId"] == "override"

    def test_the_join_keys_survive_a_span_at_its_attribute_cap(self):
        pipeline, _, _ = make(
            context={"distinct_id": "user-1", "session_id": "sess-1"},
            max_attributes_per_span=2,
        )
        pipeline.start_span("a", attributes={"x": 1, "y": 2, "z": 3}).end()
        record = queued(pipeline)[0]
        assert record.attributes["posthogDistinctId"] == "user-1"
        assert record.attributes["sessionId"] == "sess-1"
        assert record.dropped_attributes_count == 1

    def test_still_records_the_span_when_reading_context_raises(self):
        pipeline, _, _ = make()
        pipeline._get_context = mock.Mock(side_effect=RuntimeError("no context"))
        pipeline.start_span("a").end()
        assert queued(pipeline)[0].attributes == {}


class TestGating:
    def test_drops_a_span_whose_client_was_disabled_mid_trace_without_raising(self):
        client = SimpleNamespace(disabled=False, send=True)
        pipeline, _, _ = make(client=client)
        span = pipeline.start_span("a")
        client.disabled = True
        span.end()
        assert queued(pipeline) == []
        assert pipeline._live_spans == {}

    def test_a_close_that_lands_mid_start_makes_the_span_inert(self):
        pipeline, _, _ = make()
        resolve = pipeline._resolve_parent

        def close_then_resolve(parent, tracestate):
            pipeline.close()
            return resolve(parent, tracestate)

        with mock.patch.object(pipeline, "_resolve_parent", close_then_resolve):
            span = pipeline.start_span("late")
        assert span is NOOP_SPAN
        assert pipeline._live_spans == {}


class TestLiveSpanBounds:
    def test_returns_the_slot_when_building_the_span_fails(self):
        pipeline, _, _ = make(max_live_spans=1)
        with mock.patch.object(
            pipeline_module, "copy_user_attributes", side_effect=RuntimeError("no")
        ):
            assert pipeline.start_span("a") is NOOP_SPAN
        assert pipeline._live_spans == {}
        assert isinstance(pipeline.start_span("b"), RecordingSpan)

    def test_returns_an_inert_handle_once_max_live_spans_are_live(self):
        pipeline, _, _ = make(max_live_spans=2)
        a, b = pipeline.start_span("a"), pipeline.start_span("b")
        assert pipeline.start_span("c") is NOOP_SPAN
        a.end()
        b.end()

    def test_frees_the_slot_when_a_span_ends(self):
        pipeline, _, _ = make(max_live_spans=1)
        pipeline.start_span("a").end()
        assert isinstance(pipeline.start_span("b"), RecordingSpan)

    def test_never_exports_a_span_evicted_for_exceeding_max_span_age(self, clock):
        pipeline, _, _ = make(max_live_spans=1, max_span_age=10)
        leaked = pipeline.start_span("leaked")
        clock["now"] += 11
        pipeline.start_span("fresh").end()
        leaked.end()
        assert [r.name for r in queued(pipeline)] == ["fresh"]

    def test_exports_a_long_span_that_ends_while_under_the_bound(self, clock):
        pipeline, _, _ = make(max_span_age=10)
        long_running = pipeline.start_span("batch")
        clock["now"] += 11
        pipeline.start_span("probe").end()
        long_running.end()
        assert [r.name for r in queued(pipeline)] == ["probe", "batch"]

    def test_returns_the_slot_on_age_eviction_so_a_leak_cannot_disable_tracing(
        self, clock
    ):
        pipeline, _, _ = make(max_live_spans=1, max_span_age=10)
        pipeline.start_span("leaked")
        assert pipeline.start_span("refused") is NOOP_SPAN
        clock["now"] += 11
        assert isinstance(pipeline.start_span("recovered"), RecordingSpan)

    def test_ages_from_start_span_not_from_a_caller_supplied_start_time(self, clock):
        pipeline, _, _ = make(max_live_spans=1, max_span_age=10)
        backdated = pipeline.start_span("old", start_time=1)
        assert pipeline.start_span("probe") is NOOP_SPAN
        backdated.end()
        assert [r.name for r in queued(pipeline)] == ["old"]

    def test_holds_only_ids_and_floats_so_a_dropped_handle_is_collectable(self):
        pipeline, _, _ = make()
        span = pipeline.start_span("leaked")
        ref = weakref.ref(span)
        del span
        gc.collect()
        assert ref() is None
        assert all(
            isinstance(k, str) and isinstance(v, float)
            for k, v in pipeline._live_spans.items()
        )


class TestDropLog:
    def test_names_reasons_in_the_order_they_happened(self, caplog):
        caplog.set_level("WARNING", logger="posthog")
        drops = DropLog(5)
        drops.record(1, "the queue is full")
        drops.record(2, "before_span_send dropped it")
        drops.record(1, "the queue is full")
        drops.warn_if_due(force=True)
        (record,) = caplog.records
        assert record.getMessage().endswith(
            "Dropping 4 span(s): the queue is full; before_span_send dropped it"
        )

    def test_a_raising_log_handler_does_not_escape(self):
        class Raising(logging.Handler):
            def emit(self, record):
                raise RuntimeError("handler broke")

        handler = Raising()
        logging.getLogger("posthog").addHandler(handler)
        try:
            drops = DropLog(5)
            drops.record(1, "the queue is full")
            drops.warn_if_due(force=True)
        finally:
            logging.getLogger("posthog").removeHandler(handler)

    def test_a_forked_child_does_not_wait_out_the_parents_warning_interval(
        self, caplog
    ):
        caplog.set_level("WARNING", logger="posthog")
        drops = DropLog(5)
        drops.record(1, "the queue is full")
        drops.warn_if_due()
        drops.reinit_after_fork()
        drops.record(1, "the queue is full")
        drops.warn_if_due()
        assert len(caplog.records) == 2


class TestCloseAndFork:
    def test_close_counts_spans_still_open_and_drops_them_when_they_end(self, caplog):
        caplog.set_level("WARNING", logger="posthog")
        pipeline, _, _ = make()
        open_span = pipeline.start_span("open")
        pipeline.close()
        open_span.end()
        assert queued(pipeline) == []
        assert any(
            "Dropping 1 span(s): they were still open at shutdown" in r.getMessage()
            for r in caplog.records
        )

    def test_close_makes_later_spans_inert_and_closes_the_exporter(self):
        pipeline, exporter, _ = make()
        pipeline.start_span("live")
        pipeline.close()
        assert exporter.closed
        assert pipeline._live_spans == {}
        assert pipeline.start_span("late") is NOOP_SPAN

    def test_a_forked_child_drops_the_parents_live_spans(self):
        pipeline, exporter, _ = make()
        pipeline.start_span("live")
        pipeline.reinit_after_fork()
        assert pipeline._live_spans == {}
        assert exporter.reinitialized

    def test_reinit_after_fork_replaces_locks_without_acquiring_them(self):
        pipeline, _, _ = make()
        pipeline._lock.acquire()
        pipeline.reinit_after_fork()
        assert not pipeline._lock.locked()
        pipeline.start_span("a").end()


class TestLimitsReachTheExport:
    def test_bounds_names_and_attributes_with_the_configured_length(self):
        sender = FakeSender(SendOutcome("ok"))
        pipeline, _, _ = make_traces(sender=sender, max_attribute_value_length=5)
        pipeline.start_span("a long name", attributes={"k": "a long value"}).end()
        pipeline.flush()
        (span,) = sender.batches()[0]
        assert span["name"] == "a lon"
        assert span["attributes"] == [{"key": "k", "value": {"stringValue": "a lon"}}]

    def test_reports_a_spans_limit_drops_once_at_debug(self, caplog):
        caplog.set_level("DEBUG", logger="posthog")
        pipeline, _, _ = make(max_attributes_per_span=1, max_events_per_span=1)
        span = pipeline.start_span("capped", attributes={"a": 1, "b": 2})
        span.add_event("e1", {"k": 1}).add_event("e2")
        span.end()
        messages = [
            r.getMessage() for r in caplog.records if "Span limits" in r.getMessage()
        ]
        assert len(messages) == 1
        assert messages[0].endswith(
            'Span limits discarded data from "capped": 1 attributes, 1 events, '
            "0 event attributes"
        )
