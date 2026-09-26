import asyncio
import gzip
import json
import threading
import time
from unittest import mock

import pytest

import posthog
from posthog import Posthog
from posthog.client import Client
from posthog.contexts import identify_context, new_context, set_context_session
from posthog.test.tracing.helpers import SPAN_ID, TRACE_ID
from posthog.tracing._transport import OK
from posthog.tracing._span import NOOP_SPAN, RecordingSpan, Span
from posthog.version import VERSION

FAKE_API_KEY = "phc_test_key"


def make_client(**kwargs):
    kwargs.setdefault("host", "https://us.example.com")
    kwargs.setdefault("sync_mode", True)
    return Client(FAKE_API_KEY, **kwargs)


def mock_session(status_code=200):
    session = mock.Mock()
    session.post.return_value = mock.Mock(status_code=status_code, headers={})
    return session


def slow_send(requests, delay=0.2):
    """A sender that records each payload and takes ``delay`` seconds to answer."""

    def send(pipeline_client, payload):
        requests.append(payload)
        time.sleep(delay)
        return OK

    return send


@pytest.fixture(autouse=True)
def no_real_network():
    with mock.patch(
        "posthog.tracing._transport._get_session", return_value=mock_session()
    ):
        yield


def flush_and_capture(client):
    session = mock_session()
    with mock.patch("posthog.tracing._transport._get_session", return_value=session):
        client.flush()
    if not session.post.called:
        return None, None, None
    args, kwargs = session.post.call_args
    return json.loads(gzip.decompress(kwargs["data"])), args[0], kwargs


def spans_from(payload):
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def resource_from(payload):
    return {
        kv["key"]: kv["value"]
        for kv in payload["resourceSpans"][0]["resource"]["attributes"]
    }


class TestConfiguration:
    def test_is_off_until_the_traces_option_is_supplied(self):
        client = make_client()
        span = client.start_span("x")
        assert span is NOOP_SPAN
        span.end()
        assert client._traces is None
        client.shutdown()

    def test_still_runs_a_with_block_when_tracing_is_off(self):
        client = make_client()
        ran = False
        with client.start_span("job") as span:
            ran = True
            assert isinstance(span, Span)
            assert client.get_active_span() is None
        assert ran
        client.shutdown()

    def test_passes_an_inbound_trace_through_a_service_that_has_tracing_off(self):
        client = make_client()
        with client.start_span("x", parent=f"00-{TRACE_ID}-{SPAN_ID}-00") as span:
            assert client.get_active_span() is span
            assert span.traceparent() == f"00-{TRACE_ID}-{SPAN_ID}-00"
        client.shutdown()

    def test_a_span_nested_in_a_pass_through_keeps_forwarding_the_trace(self):
        client = make_client()
        inbound = f"00-{TRACE_ID}-{SPAN_ID}-00"
        with client.start_span("request", parent=inbound, tracestate="vendor=abc"):
            with client.start_span("db.query") as nested:
                assert nested.traceparent() == inbound
                assert nested.tracestate() == "vendor=abc"
                assert client.get_active_span().traceparent() == inbound
        client.shutdown()

    def test_traces_false_leaves_tracing_off(self):
        client = make_client(traces=False)
        assert client.start_span("x") is NOOP_SPAN
        assert client._traces is None
        client.shutdown()

    def test_a_parent_whose_traceparent_raises_gives_a_noop_with_tracing_off(self):
        class Hostile(Span):
            def traceparent(self):
                raise RuntimeError("no")

        client = make_client()
        assert client.start_span("x", parent=Hostile()) is NOOP_SPAN
        client.shutdown()

    def test_a_bad_traces_config_degrades_to_defaults(self):
        client = make_client(traces="nope")
        assert isinstance(client.start_span("x"), RecordingSpan)
        client.shutdown()

    def test_a_failed_init_is_not_retried_on_the_next_call(self):
        client = make_client(traces={})
        with mock.patch(
            "posthog.client.resolve_traces_config", side_effect=RuntimeError("no")
        ) as resolve:
            assert client.start_span("x") is NOOP_SPAN
            assert client.start_span("y") is NOOP_SPAN
        assert resolve.call_count == 1
        client.shutdown()

    def test_a_non_callable_hook_turns_tracing_off(self, caplog):
        caplog.set_level("ERROR", logger="posthog")
        client = make_client(traces={"before_span_send": "scrub"})
        assert client.start_span("x") is NOOP_SPAN
        assert "Error initializing traces" in caplog.text
        assert "not callable" in caplog.text
        client.shutdown()

    def test_never_starts_a_pipeline_on_a_client_without_traces(self):
        client = make_client()
        client.flush()
        client.shutdown()
        assert client._traces is None


class TestTransport:
    def test_posts_to_the_traces_endpoint_with_bearer_auth(self):
        client = make_client(traces={"service_name": "api"})
        client.start_span("x").end()
        payload, url, kwargs = flush_and_capture(client)
        assert url == "https://us.example.com/i/v1/traces"
        assert kwargs["headers"]["Authorization"] == "Bearer phc_test_key"
        assert "token=" not in url
        assert len(spans_from(payload)) == 1
        client.shutdown()

    def test_sends_the_service_name_sdk_identity_and_host_os(self):
        client = make_client(traces={"service_name": "api"})
        client.start_span("x").end()
        payload, _, _ = flush_and_capture(client)
        resource = resource_from(payload)
        assert resource["service.name"] == {"stringValue": "api"}
        assert resource["telemetry.sdk.name"] == {"stringValue": "posthog-python"}
        assert resource["telemetry.sdk.version"] == {"stringValue": VERSION}
        assert "os.name" in resource
        assert payload["resourceSpans"][0]["scopeSpans"][0]["scope"] == {
            "name": "posthog-python",
            "version": VERSION,
        }
        client.shutdown()

    def test_lets_configured_resource_attributes_override_the_host_os(self):
        client = make_client(traces={"resource_attributes": {"os.name": "Custom"}})
        client.start_span("x").end()
        payload, _, _ = flush_and_capture(client)
        assert resource_from(payload)["os.name"] == {"stringValue": "Custom"}
        client.shutdown()

    def test_exports_well_formed_ids_and_string_nanosecond_timestamps(self):
        client = make_client(traces={})
        client.start_span("x", attributes={"n": 7}).end()
        payload, _, _ = flush_and_capture(client)
        (span,) = spans_from(payload)
        assert len(span["traceId"]) == 32 and len(span["spanId"]) == 16
        assert span["startTimeUnixNano"].isdigit() and span["endTimeUnixNano"].isdigit()
        assert int(span["endTimeUnixNano"]) >= int(span["startTimeUnixNano"])
        assert {"key": "n", "value": {"intValue": "7"}} in span["attributes"]
        client.shutdown()

    def test_send_false_records_but_never_posts(self):
        client = make_client(send=False, traces={})
        client.start_span("x").end()
        _, url, _ = flush_and_capture(client)
        assert url is None
        assert client._traces._exporter._queue == []
        client.shutdown()


class TestActiveSpanContext:
    def test_nests_spans_started_inside_a_with_block(self):
        client = make_client(traces={})
        with client.start_span("parent"):
            client.start_span("child").end()
        payload, _, _ = flush_and_capture(client)
        child, parent_record = spans_from(payload)
        assert child["parentSpanId"] == parent_record["spanId"]
        assert child["traceId"] == parent_record["traceId"]
        client.shutdown()

    def test_keeps_the_span_active_across_an_await(self):
        client = make_client(traces={})

        async def handler():
            with client.start_span("request") as span:
                await asyncio.sleep(0)
                assert client.get_active_span() is span
                with client.start_span("inner"):
                    await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert client.get_active_span() is span

        asyncio.run(handler())
        payload, _, _ = flush_and_capture(client)
        inner, request = spans_from(payload)
        assert inner["parentSpanId"] == request["spanId"]
        client.shutdown()

    def test_isolates_concurrent_tasks_from_each_other(self):
        client = make_client(traces={})

        async def task(name):
            with client.start_span(name) as span:
                await asyncio.sleep(0.01)
                assert client.get_active_span() is span

        async def run():
            await asyncio.gather(task("a"), task("b"))

        asyncio.run(run())
        payload, _, _ = flush_and_capture(client)
        assert all("parentSpanId" not in s for s in spans_from(payload))
        client.shutdown()

    def test_parents_each_tasks_children_to_that_tasks_root(self):
        client = make_client(traces={})

        async def task(name):
            with client.start_span(name):
                await asyncio.sleep(0.01)
                client.start_span(name + ".child").end()

        async def run():
            await asyncio.gather(task("a"), task("b"))

        asyncio.run(run())
        payload, _, _ = flush_and_capture(client)
        spans = {s["name"]: s for s in spans_from(payload)}
        for name in ("a", "b"):
            assert spans[name + ".child"]["parentSpanId"] == spans[name]["spanId"]
            assert spans[name + ".child"]["traceId"] == spans[name]["traceId"]
        client.shutdown()

    def test_isolates_concurrent_threads_from_each_other(self):
        client = make_client(traces={})
        barrier = threading.Barrier(2, timeout=5)
        seen = {}

        def work(name):
            with client.start_span(name) as span:
                barrier.wait()
                seen[name] = client.get_active_span() is span

        threads = [threading.Thread(target=work, args=(n,)) for n in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert seen == {"a": True, "b": True}
        client.shutdown()

    def test_two_clients_do_not_share_an_active_span(self):
        first = make_client(traces={})
        second = make_client(traces={})
        with first.start_span("a"):
            assert second.get_active_span() is None
            second.start_span("b").end()
        (record,) = second._traces._exporter._queue
        assert record.parent_span_id is None
        first.shutdown()
        second.shutdown()

    def test_reads_none_outside_any_block(self):
        client = make_client(traces={})
        assert client.get_active_span() is None
        client.shutdown()


class TestAutoContext:
    def test_attaches_the_request_distinct_id_and_session_id(self):
        client = make_client(traces={})
        with new_context(fresh=True, capture_exceptions=False):
            identify_context("user-1")
            set_context_session("sess-1")
            client.start_span("x").end()
        payload, _, _ = flush_and_capture(client)
        (span,) = spans_from(payload)
        assert {"key": "posthogDistinctId", "value": {"stringValue": "user-1"}} in span[
            "attributes"
        ]
        assert {"key": "sessionId", "value": {"stringValue": "sess-1"}} in span[
            "attributes"
        ]
        client.shutdown()

    def test_omits_the_keys_outside_a_request_context(self):
        client = make_client(traces={})
        with new_context(fresh=True, capture_exceptions=False):
            client.start_span("x").end()
        payload, _, _ = flush_and_capture(client)
        (span,) = spans_from(payload)
        assert "attributes" not in span
        client.shutdown()


class TestDistributedTracing:
    def test_continues_a_trace_from_an_inbound_traceparent_and_propagates_the_flag(
        self,
    ):
        client = make_client(traces={})
        with client.start_span("x", parent=f"00-{TRACE_ID}-{SPAN_ID}-00") as span:
            outgoing = span.traceparent()
        assert outgoing.startswith(f"00-{TRACE_ID}-") and outgoing.endswith("-00")
        payload, _, _ = flush_and_capture(client)
        (record,) = spans_from(payload)
        assert record["traceId"] == TRACE_ID
        assert record["parentSpanId"] == SPAN_ID
        assert record["flags"] == 0x300
        client.shutdown()

    def test_records_a_raised_error_and_rethrows_it_unchanged(self):
        client = make_client(traces={})
        error = RuntimeError("boom")
        with pytest.raises(RuntimeError) as raised:
            with client.start_span("x"):
                raise error
        assert raised.value is error
        payload, _, _ = flush_and_capture(client)
        (record,) = spans_from(payload)
        assert record["status"] == {"code": 2, "message": "boom"}
        client.shutdown()


class TestLifecycle:
    def test_flush_drains_queued_spans(self):
        client = make_client(traces={})
        client.start_span("x").end()
        payload, _, _ = flush_and_capture(client)
        assert len(spans_from(payload)) == 1
        assert client._traces._exporter._queue == []
        client.shutdown()

    def test_flush_resolves_when_the_span_export_fails(self):
        client = make_client(traces={})
        client.start_span("x").end()
        with mock.patch(
            "posthog.tracing._transport._get_session", return_value=mock_session(500)
        ):
            client.flush()
        assert len(client._traces._exporter._queue) == 1
        client.shutdown()

    def test_flush_stops_starting_span_requests_once_its_budget_is_spent(
        self, fake_timers
    ):
        client = make_client(traces={"max_export_batch_size": 1})
        client.start_span("x").end()
        client.start_span("y").end()
        requests = []
        client._traces._exporter._send = slow_send(requests)
        client.flush(timeout_seconds=0.05)
        assert len(requests) == 1
        assert len(client._traces._exporter._queue) == 1
        client._traces._exporter._send = lambda pipeline_client, payload: OK
        client.shutdown()

    def test_flush_without_a_timeout_drains_every_queued_span(self):
        client = make_client(traces={"max_export_batch_size": 1})
        session = mock_session()
        # Spans end inside the patch: at a batch size of 1 each end arms the
        # depth trigger, whose background drain must post to this session too.
        with mock.patch(
            "posthog.tracing._transport._get_session", return_value=session
        ):
            client.start_span("x").end()
            client.start_span("y").end()
            client.flush(timeout_seconds=None)
        assert session.post.call_count == 2
        assert client._traces._exporter._queue == []
        client.shutdown()

    def test_flush_sends_spans_while_events_are_still_draining(self, fake_timers):
        client = make_client(traces={}, sync_mode=False)
        client.start_span("x").end()
        span_sent = threading.Event()

        def send(pipeline_client, payload):
            span_sent.set()
            return OK

        client._traces._exporter._send = send
        overlapped = []
        for lane in client._lanes:
            lane.flush = lambda timeout: overlapped.append(span_sent.wait(2))
        client.flush()
        assert overlapped and all(overlapped)
        client.shutdown()

    def test_flush_sends_spans_inline_when_no_thread_can_start(self, fake_timers):
        client = make_client(traces={})
        client.start_span("x").end()
        with mock.patch("posthog.client.threading.Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError(
                "can't create new thread at interpreter shutdown"
            )
            payload, _, _ = flush_and_capture(client)
        assert len(spans_from(payload)) == 1
        client.shutdown()

    def test_flush_starts_no_span_thread_when_nothing_is_queued(self, fake_timers):
        client = make_client(traces={})
        client.start_span("x").end()
        client.flush()
        with mock.patch("posthog.client.threading.Thread") as thread:
            client.flush()
        thread.assert_not_called()
        client.shutdown()

    def test_exit_flushes_the_lanes_before_an_inline_span_flush(self, fake_timers):
        client = make_client(traces={}, sync_mode=False)
        client.start_span("x").end()
        order = []
        client._traces.flush = lambda timeout: order.append("spans")
        for lane in client._lanes:
            lane.flush = lambda timeout, _lane=lane: order.append("lanes")
        with (
            mock.patch("posthog.client.threading.Thread") as thread,
            mock.patch("posthog.client._atexit_deadline", None),
        ):
            thread.return_value.start.side_effect = RuntimeError("no threads")
            client._atexit()
        assert order[0] == "lanes"
        assert order[-1] == "spans"
        client.shutdown()

    def test_shutdown_flushes_pending_spans(self):
        client = make_client(traces={})
        client.start_span("x").end()
        session = mock_session()
        with mock.patch(
            "posthog.tracing._transport._get_session", return_value=session
        ):
            client.shutdown()
        assert session.post.called
        assert client._traces._exporter._queue == []

    def test_shutdown_bounds_the_final_span_flush_and_warns_about_the_rest(
        self, fake_timers, caplog
    ):
        caplog.set_level("WARNING", logger="posthog")
        client = make_client(traces={"max_export_batch_size": 1})
        for name in ("a", "b", "c"):
            client.start_span(name).end()
        requests = []
        client._traces._exporter._send = slow_send(requests)
        with mock.patch("posthog.client._TRACES_SHUTDOWN_FLUSH_SECONDS", 0.05):
            client.shutdown()
        assert len(requests) == 1
        assert any("Discarding 2 span(s)" in r.getMessage() for r in caplog.records)

    def test_tracing_is_inert_after_shutdown(self, fake_timers):
        client = make_client(traces={})
        client.start_span("before").end()
        client.shutdown()
        assert client.start_span("late") is NOOP_SPAN
        assert client._traces._exporter._flush_timer is None

    def test_shutdown_closes_a_pipeline_still_initializing(self, fake_timers):
        client = make_client(traces={})
        shutdown = threading.Thread(target=client.shutdown)
        resolve = posthog.client.resolve_traces_config

        def resolve_while_shutting_down(*args):
            shutdown.start()
            time.sleep(0.05)
            return resolve(*args)

        with mock.patch(
            "posthog.client.resolve_traces_config", resolve_while_shutting_down
        ):
            client.start_span("racing").end()
        shutdown.join()
        assert client._traces._closed
        assert client._traces._exporter._flush_timer is None

    def test_tracing_never_starts_after_shutdown(self, fake_timers):
        client = make_client(traces={})
        client.shutdown()
        assert client.start_span("late") is NOOP_SPAN
        assert client._traces is None

    def test_exit_drains_spans_the_timer_would_have_sent(self, fake_timers):
        client = make_client(traces={}, sync_mode=False)
        client.start_span("x").end()
        session = mock_session()
        # The exit deadline is process-wide and set once; start a fresh one.
        with (
            mock.patch("posthog.tracing._transport._get_session", return_value=session),
            mock.patch("posthog.client._atexit_deadline", None),
        ):
            client._atexit()
        assert session.post.called
        # The exit hook leaves tracing open for a later shutdown().
        client.start_span("y").end()
        assert len(client._traces._exporter._queue) == 1
        session = mock_session()
        with mock.patch(
            "posthog.tracing._transport._get_session", return_value=session
        ):
            client.shutdown()
        assert session.post.called

    def test_exit_flushes_spans_alongside_events_that_use_up_the_budget(
        self, fake_timers
    ):
        client = make_client(traces={}, sync_mode=False)
        client.start_span("x").end()
        session = mock_session()

        def slow_lane_flush(timeout_seconds):
            time.sleep(0.3)

        with (
            mock.patch("posthog.tracing._transport._get_session", return_value=session),
            mock.patch("posthog.client._ATEXIT_FLUSH_TIMEOUT_SECONDS", 0.2),
            mock.patch("posthog.client._atexit_deadline", None),
            mock.patch.object(client._lanes[0], "flush", slow_lane_flush),
        ):
            client._atexit()
        assert session.post.called
        client.shutdown()

    def test_exit_does_not_wait_on_a_hung_span_request(self, fake_timers, caplog):
        caplog.set_level("WARNING", logger="posthog")
        client = make_client(traces={}, sync_mode=False)
        client.start_span("x").end()
        release = threading.Event()
        session = mock.Mock()

        def hung_post(*args, **kwargs):
            release.wait(5)
            return mock.Mock(status_code=200, headers={})

        session.post.side_effect = hung_post
        with (
            mock.patch("posthog.tracing._transport._get_session", return_value=session),
            mock.patch("posthog.client._ATEXIT_FLUSH_TIMEOUT_SECONDS", 0.2),
            mock.patch("posthog.client._atexit_deadline", None),
        ):
            try:
                started = time.monotonic()
                client._atexit()
                elapsed = time.monotonic() - started
            finally:
                release.set()
        assert session.post.called
        assert elapsed < 1
        assert any(
            "1 span(s) were still queued at exit" in r.getMessage()
            for r in caplog.records
        )
        client.shutdown()

    @pytest.mark.parametrize(
        "sync_mode, hook", [(False, "_atexit"), (True, "_atexit_spans")]
    )
    def test_exit_warns_about_spans_it_could_not_send(
        self, fake_timers, caplog, sync_mode, hook
    ):
        caplog.set_level("WARNING", logger="posthog")
        client = make_client(traces={}, sync_mode=sync_mode)
        client.start_span("x").end()
        with (
            mock.patch(
                "posthog.tracing._transport._get_session",
                return_value=mock_session(503),
            ),
            mock.patch("posthog.client._atexit_deadline", None),
        ):
            getattr(client, hook)()
        assert any(
            "1 span(s) were still queued at exit" in r.getMessage()
            for r in caplog.records
        )
        client.shutdown()

    @pytest.mark.parametrize("sync_mode", [True, False])
    def test_an_app_exit_hook_registered_earlier_still_gets_to_flush_spans(
        self, fake_timers, caplog, sync_mode
    ):
        caplog.set_level("WARNING", logger="posthog")
        hooks = []
        holder = {}
        requests = []

        with (
            mock.patch("posthog.client.atexit.register", side_effect=hooks.append),
            mock.patch("posthog.client._ATEXIT_FLUSH_TIMEOUT_SECONDS", 0.1),
            mock.patch("posthog.client._atexit_deadline", None),
        ):
            # The app's own hook, registered before the client's.
            hooks.append(lambda: holder["client"].shutdown())
            client = holder["client"] = make_client(
                traces={"max_export_batch_size": 1}, sync_mode=sync_mode
            )
            client.start_span("a").end()
            client._traces._exporter._send = slow_send(requests)
            client.start_span("b").end()
            client.start_span("c").end()
            assert len(hooks) == 2
            # atexit runs hooks last-registered first.
            for hook in reversed(hooks):
                hook()
        assert len(requests) == 3
        assert not any("Discarding" in r.getMessage() for r in caplog.records)

    def test_sync_mode_registers_the_span_exit_drain_when_tracing_starts(self):
        with mock.patch("posthog.client.atexit.register") as register:
            client = make_client(traces={}, sync_mode=True)
            register.assert_not_called()
            client.start_span("x").end()
            client.start_span("y").end()
        register.assert_called_once_with(client._atexit_spans)
        client.shutdown()

    def test_shutdown_unregisters_the_sync_mode_exit_drain(self):
        client = make_client(traces={}, sync_mode=True)
        client.start_span("x").end()
        with mock.patch("posthog.client.atexit.unregister") as unregister:
            client.shutdown()
        unregister.assert_called_once_with(client._atexit_spans)

    @pytest.mark.parametrize("traces", [False, None])
    def test_sync_mode_without_tracing_registers_no_exit_hook(self, traces):
        with mock.patch("posthog.client.atexit.register") as register:
            client = make_client(traces=traces, sync_mode=True)
            client.start_span("x").end()
        register.assert_not_called()
        client.shutdown()

    def test_the_span_exit_drain_leaves_sync_mode_events_alone(self):
        client = make_client(traces={}, sync_mode=True)
        client.start_span("x").end()
        with mock.patch("posthog.client._atexit_deadline", None):
            client._atexit_spans()
        with mock.patch("posthog.client.batch_post") as batch_post:
            client.capture("after-exit", distinct_id="d")
        batch_post.assert_called_once()
        assert batch_post.call_args[1]["batch"][0]["event"] == "after-exit"
        client.shutdown()

    @pytest.mark.parametrize("traces", [{}, None])
    def test_background_mode_registers_the_exit_hook_once(self, traces):
        with mock.patch("posthog.client.atexit.register") as register:
            client = make_client(traces=traces, sync_mode=False)
            register.assert_called_once_with(client._atexit)
            client.start_span("x").end()
        register.assert_called_once_with(client._atexit)
        client.shutdown()

    def test_a_forked_child_drops_the_inherited_queue(self):
        client = make_client(traces={})
        client.start_span("parent-span").end()
        client._reinit_after_fork()
        assert client._traces._exporter._queue == []
        client.start_span("child-span").end()
        assert [r.name for r in client._traces._exporter._queue] == ["child-span"]
        client.shutdown()

    def test_a_forked_child_does_not_inherit_the_active_span(self):
        client = make_client(traces={})
        with client.start_span("parent-span"):
            client._reinit_after_fork()
            assert client.get_active_span() is None
        client.shutdown()

    def test_an_inherited_span_exiting_in_the_child_does_not_restore_its_parent(
        self,
    ):
        client = make_client(traces={})
        with client.start_span("outer") as outer:
            with client.start_span("inner"):
                client._reinit_after_fork()
            assert client.get_active_span() is None
            child = client.start_span("child")
            child.end()
        (record,) = client._traces._exporter._queue
        assert record.name == "child"
        assert record.parent_span_id is None
        assert record.trace_id != outer.traceparent().split("-")[1]
        client.shutdown()


class TestModuleLevelApi:
    def _with_module_client(self, traces, body):
        saved = (
            posthog.default_client,
            posthog.api_key,
            posthog.host,
            posthog.sync_mode,
            posthog.traces,
        )
        posthog.default_client = None
        posthog.api_key = FAKE_API_KEY
        posthog.host = "https://us.example.com"
        posthog.sync_mode = True
        posthog.traces = traces
        try:
            body()
        finally:
            if posthog.default_client is not None:
                posthog.default_client.shutdown()
            (
                posthog.default_client,
                posthog.api_key,
                posthog.host,
                posthog.sync_mode,
                posthog.traces,
            ) = saved

    def test_module_config_flows_to_the_default_client(self):
        def body():
            with posthog.start_span("x") as span:
                assert posthog.get_active_span() is span
            payload, _, _ = flush_and_capture(posthog.default_client)
            assert resource_from(payload)["service.name"] == {
                "stringValue": "module-configured"
            }

        self._with_module_client({"service_name": "module-configured"}, body)

    def test_module_config_set_after_setup_applies_until_first_use(self):
        def body():
            posthog.setup()
            posthog.traces = {"service_name": "late-configured"}
            posthog.setup()
            posthog.start_span("x").end()
            payload, _, _ = flush_and_capture(posthog.default_client)
            assert resource_from(payload)["service.name"] == {
                "stringValue": "late-configured"
            }

        self._with_module_client(None, body)

    def test_setup_leaves_an_explicitly_configured_default_client_alone(self):
        def body():
            posthog.default_client = make_client(traces={"service_name": "explicit"})
            posthog.setup()
            assert isinstance(posthog.start_span("x"), RecordingSpan)

        self._with_module_client(None, body)

    def test_setup_does_not_retry_a_traces_init_that_failed(self):
        def body():
            with mock.patch(
                "posthog.client.resolve_traces_config", side_effect=RuntimeError("no")
            ) as resolve:
                assert posthog.start_span("x") is NOOP_SPAN
                assert posthog.start_span("y") is NOOP_SPAN
            assert resolve.call_count == 1

        self._with_module_client({"service_name": "broken"}, body)

    def test_module_start_span_is_inert_without_config(self):
        def body():
            assert posthog.start_span("x") is NOOP_SPAN
            assert posthog.get_active_span() is None

        self._with_module_client(None, body)

    def test_posthog_alias_accepts_the_traces_option(self):
        client = Posthog(
            FAKE_API_KEY, host="https://us.example.com", sync_mode=True, traces={}
        )
        assert isinstance(client.start_span("x"), RecordingSpan)
        client.shutdown()
