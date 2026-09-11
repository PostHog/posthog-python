import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from posthog.test.tracing.helpers import (
    FakeSender,
    FakeTimer,
    RealTimer,
    clock,
    fake_timers,
    make_traces,
    no_jitter,
    queued,
)
from posthog.tracing import _export as export_module
from posthog.tracing._config import DEFAULT_MAX_EXPORT_BATCH_SIZE
from posthog.tracing._export import MAX_RETRIES_PER_BATCH, MAX_RETRY_AFTER_SECONDS
from posthog.tracing._span import NOOP_SPAN
from posthog.tracing._transport import TOO_LARGE_LOCALLY, SendOutcome

__all__ = ["clock", "fake_timers", "no_jitter"]


class TestExport:
    def test_arms_an_immediate_flush_when_the_queue_reaches_the_batch_size(self):
        pipeline, sender, _ = make_traces(max_export_batch_size=2)
        pipeline.start_span("a").end()
        assert FakeTimer.instances[-1].delay == 5
        pipeline.start_span("b").end()
        timer = FakeTimer.instances[-1]
        assert timer.delay == 0
        timer.fire()
        assert len(sender.batches()) == 1
        assert len(sender.batches()[0]) == 2
        assert queued(pipeline) == []

    def test_flushes_on_the_interval_timer(self):
        pipeline, sender, _ = make_traces()
        pipeline.start_span("a").end()
        timer = FakeTimer.instances[-1]
        assert timer.delay == 5 and timer.started
        timer.fire()
        assert len(sender.payloads) == 1

    def test_sends_one_resource_and_one_scope_per_batch(self):
        pipeline, sender, _ = make_traces(service_name="api")
        pipeline.start_span("a").end()
        pipeline.flush()
        payload = sender.payloads[0]
        assert len(payload["resourceSpans"]) == 1
        assert len(payload["resourceSpans"][0]["scopeSpans"]) == 1
        attrs = {
            kv["key"]: kv["value"]
            for kv in payload["resourceSpans"][0]["resource"]["attributes"]
        }
        assert attrs["service.name"] == {"stringValue": "api"}
        assert attrs["telemetry.sdk.name"] == {"stringValue": "posthog-python"}

    def test_splits_a_backlog_across_batches(self):
        pipeline, sender, _ = make_traces(max_export_batch_size=2)
        for i in range(5):
            pipeline.start_span(str(i)).end()
        pipeline.flush()
        assert [len(b) for b in sender.batches()] == [2, 2, 1]
        assert queued(pipeline) == []

    def test_drains_spans_that_arrive_during_a_pass(self):
        pipeline, sender, _ = make_traces()

        def send_and_enqueue(client, payload):
            sender.payloads.append(payload)
            if len(sender.payloads) == 1:
                pipeline.start_span("late").end()
            return SendOutcome("ok")

        pipeline._exporter._send = send_and_enqueue
        pipeline.start_span("early").end()
        pipeline.flush()
        assert [[s["name"] for s in b] for b in sender.batches()] == [
            ["early"],
            ["late"],
        ]

    def test_does_not_re_post_on_every_span_end_while_a_flush_is_failing(self):
        pipeline, sender, _ = make_traces(
            sender=FakeSender(SendOutcome("retry-later")), max_export_batch_size=1
        )
        pipeline.start_span("a").end()
        FakeTimer.instances[-1].fire()
        assert len(sender.payloads) == 1
        pipeline.start_span("b").end()
        assert FakeTimer.instances[-1].delay > 0

    def test_a_disabled_client_discards_the_queue_instead_of_exporting(self):
        client = SimpleNamespace(disabled=False, send=True)
        pipeline, sender, _ = make_traces(client=client)
        pipeline.start_span("a").end()
        client.disabled = True
        pipeline.flush()
        assert sender.payloads == []
        assert queued(pipeline) == []

    def test_a_disabled_discard_takes_the_failed_batchs_budget_with_it(self, clock):
        client = SimpleNamespace(disabled=False, send=True)
        sender = FakeSender(SendOutcome("retry-later"), SendOutcome("ok"))
        pipeline, _, _ = make_traces(client=client, sender=sender)
        pipeline.start_span("a").end()
        pipeline.flush()
        assert pipeline._exporter._head_batch_failures == 1
        client.disabled = True
        pipeline.flush()
        client.disabled = False
        assert pipeline._exporter._head_batch_failures == 0
        assert pipeline._exporter._consecutive_failures == 0

    def test_runs_at_most_one_follow_up_pass(self):
        pipeline, sender, _ = make_traces()

        def send_and_enqueue(client, payload):
            sender.payloads.append(payload)
            pipeline.start_span("late").end()
            return SendOutcome("ok")

        pipeline._exporter._send = send_and_enqueue
        pipeline.start_span("early").end()
        pipeline.flush()
        assert len(sender.payloads) == 2
        assert len(queued(pipeline)) == 1

    def test_stops_starting_requests_once_the_deadline_passes(self, clock):
        pipeline, sender, _ = make_traces(max_export_batch_size=1)

        def send_slowly(client, payload):
            sender.payloads.append(payload)
            clock["now"] += 1
            return SendOutcome("ok")

        pipeline._exporter._send = send_slowly
        for _ in range(3):
            pipeline.start_span("a").end()
        pipeline.flush(timeout=0.1)
        assert len(sender.payloads) == 1
        assert len(queued(pipeline)) == 2

    def test_a_spent_budget_still_ships_one_batch(self):
        pipeline, sender, _ = make_traces()
        pipeline.start_span("a").end()
        pipeline.flush(timeout=0.0)
        assert len(sender.payloads) == 1
        assert queued(pipeline) == []

    def test_returns_without_draining_when_another_flush_holds_the_lock_past_the_deadline(
        self, caplog
    ):
        caplog.set_level("DEBUG", logger="posthog")
        pipeline, sender, _ = make_traces()
        pipeline.start_span("a").end()
        timer = FakeTimer.instances[-1]
        pipeline._exporter._flush_lock.acquire()
        try:
            pipeline.flush(timeout=0.01)
        finally:
            pipeline._exporter._flush_lock.release()
        assert sender.payloads == []
        assert pipeline._exporter._flush_timer is timer
        assert not timer.cancelled
        assert "another flush was still in flight" in caplog.text

    def test_the_budget_starts_once_the_lock_is_held(self, clock):
        pipeline, sender, _ = make_traces(max_export_batch_size=1)
        exporter = pipeline._exporter

        def send_slowly(client, payload):
            sender.payloads.append(payload)
            clock["now"] += 0.1
            return SendOutcome("ok")

        exporter._send = send_slowly
        for _ in range(2):
            pipeline.start_span("a").end()
        exporter._flush_lock.acquire()

        def release_after_a_while():
            time.sleep(0.05)
            clock["now"] += 1.0
            exporter._flush_lock.release()

        threading.Thread(target=release_after_a_while).start()
        pipeline.flush(timeout=0.5)
        assert len(sender.payloads) == 2
        assert queued(pipeline) == []

    def test_re_arms_after_a_timer_that_failed_to_start(self):
        class FlakyTimer(FakeTimer):
            failed = False

            def start(self):
                if self.delay == 0 and not FlakyTimer.failed:
                    FlakyTimer.failed = True
                    raise RuntimeError("can't start new thread")
                super().start()

        with mock.patch.object(threading, "Timer", FlakyTimer):
            pipeline, sender, _ = make_traces(max_export_batch_size=3)
            for _ in range(3):
                pipeline.start_span("a").end()
            interval_timer = pipeline._exporter._flush_timer
            assert interval_timer.delay == 5 and not interval_timer.cancelled
            pipeline.start_span("b").end()
            timer = pipeline._exporter._flush_timer
            assert timer is not interval_timer and timer.started and timer.delay == 0
            assert interval_timer.cancelled
            timer.fire()
        assert queued(pipeline) == []
        assert [len(b) for b in sender.batches()] == [3, 1]


class TestBackgroundDrainThreads:
    def test_at_most_one_follow_up_thread_parks_behind_an_in_flight_flush(self):
        created = []

        class RecordingTimer(RealTimer):
            def __init__(self, delay, fn):
                super().__init__(delay, fn)
                created.append(self)

        sending = threading.Event()
        release = threading.Event()
        payloads = []

        def blocking_send(client, payload):
            payloads.append(payload)
            sending.set()
            release.wait(5)
            return SendOutcome("ok")

        with mock.patch.object(threading, "Timer", RecordingTimer):
            pipeline, _, _ = make_traces(
                sender=blocking_send,
                max_export_batch_size=10,
                max_queue_size=10_000,
                flush_interval=60,
            )
            for _ in range(10):
                pipeline.start_span("a").end()
            assert sending.wait(5)
            timers_before = len(created)
            for _ in range(300):
                pipeline.start_span("b").end()
            assert len(created) - timers_before <= 2
            release.set()
            for timer in list(created):
                timer.join(5)
        assert queued(pipeline) == []
        assert (
            sum(len(p["resourceSpans"][0]["scopeSpans"][0]["spans"]) for p in payloads)
            == 310
        )


class TestExportFailures:
    def test_halves_the_batch_and_resends_the_same_spans_on_413(self):
        sender = FakeSender(
            SendOutcome("too-large"), SendOutcome("ok"), SendOutcome("ok")
        )
        pipeline, _, _ = make_traces(sender=sender, max_export_batch_size=4)
        for i in range(4):
            pipeline.start_span(str(i)).end()
        pipeline.flush()
        assert [[s["name"] for s in b] for b in sender.batches()] == [
            ["0", "1", "2", "3"],
            ["0", "1"],
            ["2", "3"],
        ]

    def test_shrinks_below_the_queue_depth_rather_than_resending_the_same_body(self):
        sender = FakeSender(SendOutcome("too-large"), SendOutcome("ok"))
        pipeline, _, _ = make_traces(sender=sender, max_export_batch_size=100)
        for i in range(4):
            pipeline.start_span(str(i)).end()
        pipeline.flush()
        assert [len(b) for b in sender.batches()] == [4, 2, 2]

    def test_ramps_the_batch_size_back_up_after_a_413_shrink(self):
        sender = FakeSender(SendOutcome("too-large"), SendOutcome("ok"))
        pipeline, _, _ = make_traces(sender=sender, max_export_batch_size=8)
        for i in range(8):
            pipeline.start_span(str(i)).end()
        pipeline.flush()
        assert pipeline._exporter._max_export_batch_size == 8
        assert [len(b) for b in sender.batches()] == [8, 4, 4]

    def test_restores_the_batch_size_once_a_413_isolates_the_oversized_span(self):
        sender = FakeSender(*([SendOutcome("too-large")] * 4), SendOutcome("ok"))
        pipeline, _, _ = make_traces(sender=sender, max_export_batch_size=8)
        for i in range(8):
            pipeline.start_span(str(i)).end()
        pipeline.flush()
        assert [len(b) for b in sender.batches()] == [8, 4, 2, 1, 7]
        assert pipeline._exporter._max_export_batch_size == 8
        assert queued(pipeline) == []

    def test_a_batch_measured_too_large_locally_splits_only_that_drain(self):
        sender = FakeSender(TOO_LARGE_LOCALLY, SendOutcome("ok"))
        pipeline, _, _ = make_traces(sender=sender, max_export_batch_size=8)
        for i in range(8):
            pipeline.start_span(str(i)).end()
        pipeline.flush()
        assert [len(b) for b in sender.batches()] == [8, 4, 4]
        # The next drain starts at full size; after a 413 it would still be
        # ramping back up from 4.
        assert pipeline._exporter._max_export_batch_size == 8
        for i in range(8):
            pipeline.start_span(str(i)).end()
        pipeline.flush()
        assert [len(b) for b in sender.batches()][3:] == [8]

    def test_drops_a_single_span_the_server_rejects_as_too_large(self, caplog):
        caplog.set_level("WARNING", logger="posthog")
        pipeline, sender, _ = make_traces(sender=FakeSender(SendOutcome("too-large")))
        pipeline.start_span("huge").end()
        pipeline.flush()
        assert queued(pipeline) == []
        assert "too large" in caplog.text

    def test_keeps_spans_queued_on_a_retriable_failure(self):
        pipeline, sender, _ = make_traces(sender=FakeSender(SendOutcome("retry-later")))
        pipeline.start_span("a").end()
        pipeline.flush()
        assert len(queued(pipeline)) == 1
        assert len(sender.payloads) == 1

    def test_does_not_resend_a_refused_batch_in_the_same_flush(self):
        sender = FakeSender(SendOutcome("ok"), SendOutcome("retry-later", 30))
        pipeline, _, _ = make_traces(sender=sender, max_export_batch_size=2)
        for _ in range(4):
            pipeline.start_span("a").end()
        pipeline.flush()
        assert len(sender.payloads) == 2
        assert len(queued(pipeline)) == 2

    def test_backs_off_exponentially_while_sends_keep_failing(self, clock):
        pipeline, _, _ = make_traces(sender=FakeSender(SendOutcome("retry-later")))
        pipeline.start_span("a").end()
        delays = []
        for _ in range(6):
            pipeline.flush()
            delays.append(FakeTimer.instances[-1].delay)
            clock["now"] += 100
        assert delays == [5, 10, 20, 30, 30, 30]

    def test_returns_to_the_base_interval_after_a_send_succeeds(self, clock):
        sender = FakeSender(
            SendOutcome("retry-later"), SendOutcome("retry-later"), SendOutcome("ok")
        )
        pipeline, _, _ = make_traces(sender=sender)
        pipeline.start_span("a").end()
        pipeline.flush()
        clock["now"] += 100
        pipeline.flush()
        assert FakeTimer.instances[-1].delay == 10
        clock["now"] += 100
        pipeline.flush()
        pipeline.start_span("b").end()
        assert FakeTimer.instances[-1].delay == 5

    def test_drops_a_poison_batch_rather_than_wedging_the_queue(self):
        sender = FakeSender(SendOutcome("fatal"), SendOutcome("ok"))
        pipeline, _, _ = make_traces(sender=sender, max_export_batch_size=1)
        pipeline.start_span("poison").end()
        pipeline.start_span("fine").end()
        pipeline.flush()
        assert [[s["name"] for s in b] for b in sender.batches()] == [
            ["poison"],
            ["fine"],
        ]
        assert queued(pipeline) == []

    def test_does_not_surface_a_transport_failure_through_span_end(self):
        def explode(client, payload):
            raise RuntimeError("transport broke")

        pipeline, _, _ = make_traces(sender=explode, max_export_batch_size=1)
        pipeline.start_span("a").end()
        FakeTimer.instances[-1].fire()

    def test_never_returns_a_span_it_failed_to_encode(self):
        pipeline, sender, _ = make_traces()
        pipeline.start_span("a").end()
        with mock.patch.object(
            export_module, "build_otlp_span", side_effect=RuntimeError("bad")
        ):
            pipeline.flush()
        assert sender.payloads == []
        assert queued(pipeline) == []

    def test_counts_a_span_it_failed_to_encode_once(self, caplog):
        caplog.set_level("WARNING", logger="posthog")
        sender = FakeSender(SendOutcome("fatal"))
        pipeline, _, _ = make_traces(sender=sender, max_export_batch_size=2)
        pipeline.start_span("bad").end()
        pipeline.start_span("fine").end()
        encode = export_module.build_otlp_span

        def encode_unless_bad(record):
            if record.name == "bad":
                raise RuntimeError("bad")
            return encode(record)

        with mock.patch.object(export_module, "build_otlp_span", encode_unless_bad):
            pipeline.flush()
        assert [[s["name"] for s in b] for b in sender.batches()] == [["fine"]]
        assert queued(pipeline) == []
        (record,) = [r for r in caplog.records if "Dropping" in r.getMessage()]
        assert "Dropping 2 span(s)" in record.getMessage()


class TestDroppedBatchesEndTheFailureSequence:
    @pytest.mark.parametrize(
        "outcome", [SendOutcome("fatal"), SendOutcome("too-large")]
    )
    def test_a_dropped_batch_re_enables_the_depth_trigger(self, clock, outcome):
        # A batch that is dropped rather than retried leaves nothing to back
        # off for, so the next full batch goes out without waiting.
        sender = FakeSender(SendOutcome("retry-later"), outcome, SendOutcome("ok"))
        pipeline, _, _ = make_traces(sender=sender, max_export_batch_size=1)
        pipeline.start_span("a").end()
        pipeline.flush()
        clock["now"] += 100
        pipeline.flush()
        assert pipeline._exporter._consecutive_failures == 0
        pipeline.start_span("b").end()
        assert FakeTimer.instances[-1].delay == 0


class TestRetryBudget:
    def test_drops_a_batch_the_endpoint_keeps_refusing_and_moves_to_the_next_one(
        self, clock
    ):
        sender = FakeSender(
            *([SendOutcome("retry-later")] * MAX_RETRIES_PER_BATCH), SendOutcome("ok")
        )
        pipeline, _, _ = make_traces(sender=sender, max_export_batch_size=1)
        pipeline.start_span("stuck").end()
        pipeline.start_span("next").end()
        for _ in range(MAX_RETRIES_PER_BATCH):
            pipeline.flush()
            clock["now"] += 100
        assert [[s["name"] for s in b] for b in sender.batches()][-1] == ["next"]
        assert queued(pipeline) == []

    def test_a_dropped_batch_keeps_the_backoff_for_the_next_one(self, clock):
        sender = FakeSender(*([SendOutcome("retry-later")] * MAX_RETRIES_PER_BATCH))
        pipeline, _, _ = make_traces(sender=sender, max_export_batch_size=1)
        pipeline.start_span("stuck").end()
        pipeline.start_span("next").end()
        for _ in range(MAX_RETRIES_PER_BATCH):
            pipeline.flush()
            clock["now"] += 100
        exporter = pipeline._exporter
        assert [s.name for s in queued(pipeline)] == ["next"]
        assert exporter._consecutive_failures >= MAX_RETRIES_PER_BATCH
        assert exporter._head_batch_failures == 1
        pipeline.start_span("c").end()
        assert FakeTimer.instances[-1].delay > 0

    def test_charges_the_budget_once_per_backoff_window_not_per_attempt(self, clock):
        pipeline, _, _ = make_traces(sender=FakeSender(SendOutcome("retry-later")))
        pipeline.start_span("a").end()
        for _ in range(MAX_RETRIES_PER_BATCH * 3):
            pipeline.flush()
        assert pipeline._exporter._head_batch_failures == 1
        assert len(queued(pipeline)) == 1

    def test_gives_the_halved_batch_its_own_budget_after_a_413(self, clock):
        sender = FakeSender(
            SendOutcome("retry-later"),
            SendOutcome("too-large"),
            SendOutcome("retry-later"),
        )
        pipeline, _, _ = make_traces(sender=sender, max_export_batch_size=2)
        pipeline.start_span("a").end()
        pipeline.start_span("b").end()
        pipeline.flush()
        clock["now"] += 100
        pipeline.flush()
        assert pipeline._exporter._head_batch_failures == 1
        assert pipeline._exporter._head_batch_size == 1

    def test_does_not_let_a_failing_head_grow_to_sweep_in_fresh_spans(self, clock):
        sender = FakeSender(SendOutcome("retry-later"))
        pipeline, _, _ = make_traces(sender=sender, max_export_batch_size=4)
        pipeline.start_span("a").end()
        pipeline.flush()
        pipeline.start_span("b").end()
        clock["now"] += 100
        pipeline.flush()
        assert [len(b) for b in sender.batches()] == [1, 1]


class TestRetryAfter:
    def test_lengthens_the_backoff_when_the_endpoint_asks_for_a_longer_wait(
        self, clock
    ):
        pipeline, _, _ = make_traces(sender=FakeSender(SendOutcome("retry-later", 20)))
        pipeline.start_span("a").end()
        pipeline.flush()
        assert FakeTimer.instances[-1].delay == 20

    def test_never_shortens_the_backoff(self, clock):
        pipeline, _, _ = make_traces(
            sender=FakeSender(SendOutcome("retry-later", 1)), flush_interval=10
        )
        pipeline.start_span("a").end()
        pipeline.flush()
        assert FakeTimer.instances[-1].delay == 10

    def test_clamps_an_oversized_retry_after(self, clock):
        pipeline, _, _ = make_traces(
            sender=FakeSender(SendOutcome("retry-later", 3600))
        )
        pipeline.start_span("a").end()
        pipeline.flush()
        assert FakeTimer.instances[-1].delay == MAX_RETRY_AFTER_SECONDS

    def test_an_explicit_flush_inside_the_window_still_sends_but_is_not_charged(
        self, clock
    ):
        pipeline, sender, _ = make_traces(
            sender=FakeSender(SendOutcome("retry-later", 30))
        )
        pipeline.start_span("a").end()
        pipeline.flush()
        clock["now"] += 1
        pipeline.flush()
        assert len(sender.payloads) == 2
        assert pipeline._exporter._head_batch_failures == 1

    def test_a_refusal_inside_an_open_window_is_not_charged(self, clock):
        # Charged at t=0 with a 5s backoff; an uncharged refusal at t=2 opens a
        # 30s window. A flush at t=6 is past the charge point but inside the
        # window the endpoint asked for, so it is caller-driven and exempt.
        sender = FakeSender(
            SendOutcome("retry-later"),
            SendOutcome("retry-later", 30),
            SendOutcome("retry-later"),
        )
        pipeline, _, _ = make_traces(sender=sender)
        pipeline.start_span("a").end()
        pipeline.flush()
        clock["now"] += 2
        pipeline.flush()
        clock["now"] += 4
        pipeline.flush()
        assert len(sender.payloads) == 3
        assert pipeline._exporter._head_batch_failures == 1

    def test_a_longer_retry_after_mid_window_extends_the_deadline(self, clock):
        window = export_module._RetryAfterWindow()
        window.record(SendOutcome("retry-later", 10))
        clock["now"] += 5
        window.record(SendOutcome("retry-later", 20))
        assert window.remaining() == 20

    def test_a_shorter_retry_after_mid_window_does_not_cut_the_wait(self, clock):
        window = export_module._RetryAfterWindow()
        window.record(SendOutcome("retry-later", 25))
        clock["now"] += 5
        window.record(SendOutcome("retry-later", 1))
        assert window.remaining() == 20

    def test_repeated_refusals_cannot_hold_the_window_past_the_ceiling(self, clock):
        window = export_module._RetryAfterWindow()
        window.record(SendOutcome("retry-later", MAX_RETRY_AFTER_SECONDS))
        clock["now"] += MAX_RETRY_AFTER_SECONDS - 10
        window.record(SendOutcome("retry-later", MAX_RETRY_AFTER_SECONDS))
        # Extended only as far as the ceiling measured from first install.
        assert window.remaining() == 10
        clock["now"] += 10
        assert not window.is_open()
        # Closed at the ceiling; the next refusal installs a new window.
        window.record(SendOutcome("retry-later", 20))
        assert window.remaining() == 20

    def test_a_refusal_naming_no_wait_leaves_an_open_window_alone(self, clock):
        window = export_module._RetryAfterWindow()
        window.record(SendOutcome("retry-later", 20))
        clock["now"] += 5
        window.record(SendOutcome("retry-later"))
        assert window.remaining() == 15

    def test_suppresses_the_depth_trigger_while_the_window_is_open(self, clock):
        # The failure count is back to 0 after the single-span drop, so only
        # the open window holds the depth trigger back.
        sender = FakeSender(
            SendOutcome("retry-later", 30), SendOutcome("too-large"), SendOutcome("ok")
        )
        pipeline, _, _ = make_traces(sender=sender, max_export_batch_size=1)
        pipeline.start_span("a").end()
        pipeline.flush()
        clock["now"] += 1
        pipeline.flush()
        assert pipeline._exporter._consecutive_failures == 0
        assert pipeline._exporter._retry_after.is_open()
        pipeline.start_span("b").end()
        assert FakeTimer.instances[-1].delay > 0

    def test_a_success_closes_the_window(self, clock):
        sender = FakeSender(SendOutcome("retry-later", 30), SendOutcome("ok"))
        pipeline, _, _ = make_traces(sender=sender)
        pipeline.start_span("a").end()
        pipeline.flush()
        clock["now"] += 1
        pipeline.flush()
        assert not pipeline._exporter._retry_after.is_open()

    def test_a_non_retriable_response_closes_the_window(self, clock):
        sender = FakeSender(SendOutcome("retry-later", 30), SendOutcome("fatal"))
        pipeline, _, _ = make_traces(sender=sender)
        pipeline.start_span("a").end()
        pipeline.flush()
        clock["now"] += 1
        pipeline.flush()
        assert not pipeline._exporter._retry_after.is_open()

    def test_a_too_large_response_leaves_the_window_open(self, clock):
        sender = FakeSender(SendOutcome("retry-later", 30), SendOutcome("too-large"))
        pipeline, _, _ = make_traces(sender=sender)
        pipeline.start_span("a").end()
        pipeline.flush()
        clock["now"] += 1
        pipeline.flush()
        assert pipeline._exporter._retry_after.remaining() == 29

    def test_retiring_a_batch_inside_the_window_sends_nothing_more_that_pass(
        self, clock
    ):
        sender = FakeSender(SendOutcome("retry-later", 30))
        pipeline, _, _ = make_traces(sender=sender, max_export_batch_size=1)
        pipeline.start_span("stuck").end()
        pipeline.start_span("next").end()
        pipeline._exporter._head_batch_failures = MAX_RETRIES_PER_BATCH - 1
        pipeline._exporter._head_batch_size = 1
        pipeline.flush()
        assert [[s["name"] for s in b] for b in sender.batches()] == [["stuck"]]
        assert [r.name for r in queued(pipeline)] == ["next"]

    def test_close_cancels_the_timer(self, clock):
        pipeline, _, _ = make_traces(sender=FakeSender(SendOutcome("retry-later", 30)))
        pipeline.start_span("a").end()
        pipeline.flush()
        pipeline.close()
        assert pipeline._exporter._flush_timer is None

    def test_a_timer_superseded_while_waiting_for_the_flush_lock_does_not_send(
        self, clock
    ):
        # A depth-triggered timer queued behind an in-flight flush that then
        # installed a Retry-After window must not send inside that window.
        sender = FakeSender(SendOutcome("retry-later", 10))
        pipeline, _, _ = make_traces(sender=sender, max_export_batch_size=1)
        exporter = pipeline._exporter
        stale = []

        def send_and_trigger(client, payload):
            sender.payloads.append(payload)
            pipeline.start_span("mid-flight").end()
            stale.append(exporter._flush_timer)
            return SendOutcome("retry-later", 10)

        exporter._send = send_and_trigger
        pipeline.start_span("a").end()
        pipeline.flush()
        assert stale[0] is not None and stale[0] is not exporter._flush_timer
        exporter.flush(_timer=stale[0])
        assert len(sender.payloads) == 1


class TestTimerRearm:
    def test_keeps_a_later_timer_a_span_armed_mid_pass(self, clock):
        # Two failures put the backoff at 10s. A span ending mid-pass arms at
        # that delay; the pass then succeeds, and its 5s re-arm must not pull
        # the pending timer in.
        sender = FakeSender(SendOutcome("retry-later"))
        pipeline, _, _ = make_traces(sender=sender, flush_interval=5)
        exporter = pipeline._exporter
        pipeline.start_span("a").end()
        pipeline.flush()
        clock["now"] += 100
        pipeline.flush()
        assert exporter._consecutive_failures == 2

        def succeed_with_a_span_ending(client, payload):
            pipeline.start_span("mid-pass").end()
            return SendOutcome("ok")

        exporter._send = succeed_with_a_span_ending
        clock["now"] += 100
        pipeline.flush()
        live = [t for t in FakeTimer.instances if not t.cancelled]
        assert [t.delay for t in live] == [10]
        assert live[0] is exporter._flush_timer

    def test_replaces_an_earlier_timer_with_a_longer_backoff(self, clock):
        sender = FakeSender(SendOutcome("retry-later", 20))
        pipeline, _, _ = make_traces(sender=sender, flush_interval=5)
        exporter = pipeline._exporter

        def fail_with_a_span_ending(client, payload):
            pipeline.start_span("mid-pass").end()
            return SendOutcome("retry-later", 20)

        exporter._send = fail_with_a_span_ending
        pipeline.start_span("a").end()
        pipeline.flush()
        assert exporter._flush_timer.delay == 20
        assert [t.delay for t in FakeTimer.instances if not t.cancelled] == [20]

    def test_a_depth_trigger_fired_mid_pass_still_drains_at_once(self):
        # Two spans end during each send, so a full batch waits after the
        # follow-up pass; it must go now, not an interval later.
        pipeline, sender, _ = make_traces(max_export_batch_size=2, flush_interval=5)
        exporter = pipeline._exporter

        def send_with_spans_ending(client, payload):
            sender.payloads.append(payload)
            if len(sender.payloads) <= 2:
                pipeline.start_span("mid-pass").end()
                pipeline.start_span("mid-pass").end()
            return SendOutcome("ok")

        exporter._send = send_with_spans_ending
        pipeline.start_span("a").end()
        pipeline.start_span("b").end()
        pipeline.flush()
        assert len(sender.payloads) == 2
        assert len(queued(pipeline)) == 2
        assert exporter._flush_timer.delay == 0
        exporter._flush_timer.fire()
        assert queued(pipeline) == []


class TestRetryBudgetResets:
    def test_a_success_gives_the_next_failure_a_full_budget(self, clock):
        sender = FakeSender(
            SendOutcome("retry-later"), SendOutcome("ok"), SendOutcome("retry-later")
        )
        pipeline, _, _ = make_traces(sender=sender, max_export_batch_size=1)
        pipeline.start_span("a").end()
        pipeline.flush()
        clock["now"] += 100
        pipeline.flush()
        pipeline.start_span("b").end()
        pipeline.flush()
        assert pipeline._exporter._head_batch_failures == 1

    def test_a_rejected_batch_gives_the_next_one_a_full_budget(self, clock):
        sender = FakeSender(
            SendOutcome("retry-later"), SendOutcome("fatal"), SendOutcome("retry-later")
        )
        pipeline, _, _ = make_traces(sender=sender, max_export_batch_size=1)
        pipeline.start_span("a").end()
        pipeline.start_span("b").end()
        pipeline.flush()
        clock["now"] += 100
        pipeline.flush()
        assert [r.name for r in queued(pipeline)] == ["b"]
        assert pipeline._exporter._head_batch_failures == 1

    def test_retires_the_head_after_its_windows_are_served_out(self, clock):
        # Each Retry-After window elapses before the next attempt, so every
        # refusal is new evidence and is charged.
        sender = FakeSender(SendOutcome("retry-later", 20))
        pipeline, _, _ = make_traces(sender=sender, max_export_batch_size=1)
        pipeline.start_span("stuck").end()
        for _ in range(MAX_RETRIES_PER_BATCH):
            pipeline.flush()
            clock["now"] += 100
        assert queued(pipeline) == []
        assert len(sender.payloads) == MAX_RETRIES_PER_BATCH


class TestSenderFailures:
    def test_a_sender_that_raises_backs_off_like_a_retriable_failure(self, clock):
        def explode(client, payload):
            raise UnicodeEncodeError("latin-1", "x", 0, 1, "bad header")

        pipeline, _, _ = make_traces(sender=explode, flush_interval=10)
        pipeline.start_span("a").end()
        pipeline.flush()
        assert [r.name for r in queued(pipeline)] == ["a"]
        assert pipeline._exporter._consecutive_failures == 1
        assert pipeline._exporter._head_batch_failures == 1
        assert FakeTimer.instances[-1].delay == 10

    def test_stops_draining_when_the_client_is_disabled_mid_pass(self, caplog):
        caplog.set_level("WARNING", logger="posthog")
        client = SimpleNamespace(disabled=False, send=True)
        pipeline, sender, _ = make_traces(client=client, max_export_batch_size=1)

        def send_and_disable(c, payload):
            sender.payloads.append(payload)
            client.disabled = True
            return SendOutcome("ok")

        pipeline._exporter._send = send_and_disable
        for name in ("a", "b", "c"):
            pipeline.start_span(name).end()
        pipeline.flush()
        assert len(sender.payloads) == 1
        assert queued(pipeline) == []
        assert any("the client is disabled" in r.getMessage() for r in caplog.records)


class TestJitter:
    @pytest.fixture(autouse=True)
    def no_jitter(self):
        # Overrides the module fixture: these tests draw real jitter.
        yield

    def test_spreads_the_backoff_by_up_to_a_quarter(self, clock):
        pipeline, _, _ = make_traces(
            sender=FakeSender(SendOutcome("retry-later")), flush_interval=10
        )
        pipeline.start_span("a").end()
        with mock.patch.object(export_module.random, "random", return_value=0.0):
            pipeline.flush()
        assert FakeTimer.instances[-1].delay == pytest.approx(7.5)
        clock["now"] += 100
        with mock.patch.object(export_module.random, "random", return_value=1.0):
            pipeline.flush()
        assert FakeTimer.instances[-1].delay == pytest.approx(25)

    def test_retry_after_is_a_floor_under_the_jittered_delay(self, clock):
        pipeline, _, _ = make_traces(
            sender=FakeSender(SendOutcome("retry-later", 10)), flush_interval=10
        )
        pipeline.start_span("a").end()
        with mock.patch.object(export_module.random, "random", return_value=0.0):
            pipeline.flush()
        assert FakeTimer.instances[-1].delay == 10

    def test_the_interval_is_not_jittered_when_nothing_has_failed(self):
        pipeline, _, _ = make_traces(flush_interval=10)
        with mock.patch.object(export_module.random, "random", return_value=0.0):
            pipeline.start_span("a").end()
        assert FakeTimer.instances[-1].delay == 10


class TestDropAccounting:
    def test_warns_once_per_interval_with_the_total_and_every_reason(
        self, clock, caplog
    ):
        caplog.set_level("WARNING", logger="posthog")
        pipeline, _, _ = make_traces(
            max_live_spans=1, max_export_batch_size=1, max_queue_size=1
        )
        held = pipeline.start_span("held")
        pipeline.start_span("refused-1")
        pipeline.start_span("refused-2")
        warnings = [r for r in caplog.records if "Dropping" in r.getMessage()]
        assert len(warnings) == 1
        assert "Dropping 1 span(s)" in warnings[0].getMessage()
        clock["now"] += 6
        pipeline.start_span("refused-3")
        warnings = [r for r in caplog.records if "Dropping" in r.getMessage()]
        assert len(warnings) == 2
        assert "Dropping 2 span(s)" in warnings[1].getMessage()
        assert "live-span limit" in warnings[1].getMessage()
        held.end()

    def test_every_flush_reports_its_drops_without_waiting_out_the_interval(
        self, clock, caplog
    ):
        caplog.set_level("WARNING", logger="posthog")
        pipeline, _, _ = make_traces(sender=FakeSender(SendOutcome("fatal")))
        for _ in range(2):
            pipeline.start_span("poison").end()
            pipeline.flush()
        warnings = [r for r in caplog.records if "Dropping" in r.getMessage()]
        assert len(warnings) == 2


class TestResetWarnings:
    def test_warns_with_the_count_when_it_discards_queued_spans(self, caplog):
        caplog.set_level("WARNING", logger="posthog")
        pipeline, _, _ = make_traces()
        pipeline.start_span("a").end()
        pipeline.start_span("b").end()
        pipeline.close()
        assert any("Discarding 2 span(s)" in r.getMessage() for r in caplog.records)

    def test_is_silent_when_nothing_was_queued(self, caplog):
        caplog.set_level("WARNING", logger="posthog")
        pipeline, _, _ = make_traces()
        pipeline.close()
        assert not caplog.records


class TestQueueBound:
    def test_drops_the_incoming_span_when_the_queue_is_full(self, caplog):
        caplog.set_level("WARNING", logger="posthog")
        pipeline, _, _ = make_traces(max_export_batch_size=2, max_queue_size=2)
        for name in ("first", "second", "third"):
            pipeline.start_span(name).end()
        assert [r.name for r in queued(pipeline)] == ["first", "second"]
        assert any("the queue is full" in r.getMessage() for r in caplog.records)


class TestCloseAndFork:
    def test_close_clears_the_queue_and_cancels_the_timer(self):
        pipeline, _, _ = make_traces()
        pipeline.start_span("done").end()
        pipeline.close()
        assert queued(pipeline) == []
        assert FakeTimer.instances[-1].cancelled
        assert pipeline._exporter._flush_timer is None

    def test_close_abandons_an_in_flight_pass_and_records_nothing_after(self):
        pipeline, sender, _ = make_traces(max_export_batch_size=1)

        def send_and_close(client, payload):
            sender.payloads.append(payload)
            pipeline.close()
            assert pipeline.start_span("after-close") is NOOP_SPAN
            return SendOutcome("ok")

        pipeline._exporter._send = send_and_close
        pipeline.start_span("a").end()
        pipeline.start_span("b").end()
        pipeline.flush()
        assert len(sender.payloads) == 1
        assert queued(pipeline) == []

    def test_a_forked_child_drops_the_inherited_queue_and_timer(self):
        pipeline, _, _ = make_traces()
        pipeline.start_span("parent-span").end()
        assert queued(pipeline) and pipeline._exporter._flush_timer is not None
        pipeline._exporter._max_export_batch_size = 1
        pipeline.reinit_after_fork()
        assert queued(pipeline) == []
        assert pipeline._exporter._flush_timer is None
        assert (
            pipeline._exporter._max_export_batch_size == DEFAULT_MAX_EXPORT_BATCH_SIZE
        )
        pipeline.start_span("child-span").end()
        assert [r.name for r in queued(pipeline)] == ["child-span"]


class TestResourceAttributes:
    def test_bounds_resource_attributes_on_every_batch(self):
        sender = FakeSender(SendOutcome("ok"))
        pipeline, _, _ = make_traces(
            sender=sender,
            max_attribute_value_length=5,
            resource_attributes={"team": "platform-infrastructure"},
        )
        pipeline.start_span("a").end()
        pipeline.flush()
        resource = {
            kv["key"]: kv["value"]
            for kv in sender.payloads[0]["resourceSpans"][0]["resource"]["attributes"]
        }
        assert resource["team"] == {"stringValue": "platf"}


def waits_advance(clock, pipeline):
    """Make the exporter's backoff wait move the fake clock instead of sleeping."""
    waited = []

    def wait(seconds):
        waited.append(seconds)
        clock["now"] += seconds
        return False

    pipeline._exporter._wait_for_retry = wait
    return waited


class TestRetryWithinBudget:
    def test_retries_a_retriable_failure_after_its_backoff(self, clock):
        sender = FakeSender(SendOutcome("retry-later"), SendOutcome("ok"))
        pipeline, _, _ = make_traces(sender=sender)
        waited = waits_advance(clock, pipeline)
        pipeline.start_span("a").end()
        pipeline.flush(timeout=30)
        assert len(sender.payloads) == 2
        assert waited == [5]
        assert queued(pipeline) == []

    def test_makes_a_last_attempt_at_the_deadline(self, clock):
        sender = FakeSender(SendOutcome("retry-later"))
        pipeline, _, _ = make_traces(sender=sender)
        waited = waits_advance(clock, pipeline)
        pipeline.start_span("a").end()
        pipeline.flush(timeout=12)
        # Attempts at 0, 5 and 12: the second backoff of 10 is cut to the budget.
        assert len(sender.payloads) == 3
        assert waited == [5, 7]
        assert len(queued(pipeline)) == 1

    def test_honours_a_retry_after_within_the_budget(self, clock):
        sender = FakeSender(SendOutcome("retry-later", 60), SendOutcome("ok"))
        pipeline, _, _ = make_traces(sender=sender)
        waited = waits_advance(clock, pipeline)
        pipeline.start_span("a").end()
        pipeline.flush(timeout=40)
        assert waited == [MAX_RETRY_AFTER_SECONDS]
        assert queued(pipeline) == []

    def test_a_timer_flush_does_not_retry(self, clock):
        sender = FakeSender(SendOutcome("retry-later"))
        pipeline, _, _ = make_traces(sender=sender)
        waited = waits_advance(clock, pipeline)
        pipeline.start_span("a").end()
        FakeTimer.instances[-1].fire()
        assert len(sender.payloads) == 1
        assert waited == []

    def test_a_flush_without_a_timeout_does_not_retry(self, clock):
        sender = FakeSender(SendOutcome("retry-later"))
        pipeline, _, _ = make_traces(sender=sender)
        waited = waits_advance(clock, pipeline)
        pipeline.start_span("a").end()
        pipeline.flush()
        assert len(sender.payloads) == 1
        assert waited == []

    def test_close_cuts_the_wait_short(self, clock):
        sender = FakeSender(SendOutcome("retry-later"))
        pipeline, _, _ = make_traces(sender=sender)
        exporter = pipeline._exporter
        pipeline.start_span("a").end()

        def close_during_wait(seconds):
            exporter.close()
            return True

        exporter._wait_for_retry = close_during_wait
        pipeline.flush(timeout=30)
        assert len(sender.payloads) == 1

    def test_the_real_wait_returns_when_close_is_called(self):
        pipeline, _, _ = make_traces()
        exporter = pipeline._exporter
        threading.Thread(target=lambda: (time.sleep(0.05), exporter.close())).start()
        started = time.monotonic()
        assert exporter._wait_for_retry(5) is True
        assert time.monotonic() - started < 2
