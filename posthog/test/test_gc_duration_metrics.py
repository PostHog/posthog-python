import gc
import os
import sys
import weakref
from types import SimpleNamespace
from unittest import mock

import pytest
from google.protobuf.json_format import ParseDict
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)

from .. import metrics_capture
from . import test_runtime_metrics as runtime_tests
from .test_metrics import sent_payload
from .test_runtime_metrics import attributes, exported_metrics

native = runtime_tests.native
runtime_client = runtime_tests.runtime_client

NAME = "app.cpython.gc.duration"


def collection(module, monkeypatch, generation=0, duration_ms=2.5):
    clock = mock.Mock(return_value=100_000_000)
    monkeypatch.setattr(module, "monotonic_ns", clock, raising=False)
    callbacks = list(module.gc_callbacks)
    for callback in callbacks:
        callback("start", {"generation": generation})
    clock.return_value += int(duration_ms * 1_000_000)
    for callback in callbacks:
        callback("stop", {"generation": generation})


def test_gc_duration_is_deferred_histogram_with_process_identity(
    runtime_client, native, monkeypatch
):
    module = native[0]
    monkeypatch.setattr(module, "time", lambda: 1234.5)
    client, session = runtime_client()
    assert len(module.gc_callbacks) == 1
    start_ms = client.metrics._runtime_metrics.start_time_ms
    collection(module, monkeypatch, generation=2, duration_ms=12.5)
    assert not session.post.called
    client.metrics.flush()
    metric = exported_metrics(session)[NAME]
    assert metric["unit"] == "ms"
    assert metric["histogram"]["aggregationTemporality"] == 1
    point = metric["histogram"]["dataPoints"][0]
    assert point["startTimeUnixNano"] == f"{start_ms}000000"
    assert point["count"] == 1
    assert point["sum"] == point["min"] == point["max"] == 12.5
    assert sum(point["bucketCounts"]) == 1
    assert attributes(point) == {
        **client.metrics._runtime_metrics.attributes,
        "cpython.gc.generation": 2,
    }
    ParseDict(sent_payload(session), ExportMetricsServiceRequest())
    client.metrics.flush()
    assert NAME not in exported_metrics(session)


def test_shutdown_removes_only_own_callback_and_flushes_duration(
    runtime_client, native, monkeypatch
):
    module = native[0]
    application_callback = mock.Mock()
    module.gc_callbacks.append(application_callback)
    client, session = runtime_client()
    collection(module, monkeypatch)
    client.shutdown()
    assert module.gc_callbacks == [application_callback]
    assert exported_metrics(session)[NAME]["histogram"]["dataPoints"][0]["count"] == 1
    client.shutdown()
    assert module.gc_callbacks == [application_callback]


def test_gc_callbacks_not_installed_without_runtime_opt_in(runtime_client, native):
    runtime_client(metrics={})
    assert native[0].gc_callbacks == []


def test_gc_duration_callback_does_not_wait_for_collection_lock(
    runtime_client, native, monkeypatch
):
    client, session = runtime_client()
    monitor = client.metrics._runtime_metrics._gc_duration
    with monitor._lock:
        collection(native[0], monkeypatch)
    client.metrics.flush()
    assert NAME not in exported_metrics(session)


def test_gc_durations_survive_send_retry(runtime_client, native, monkeypatch):
    client, session = runtime_client()
    collection(native[0], monkeypatch, duration_ms=2)
    session.post.return_value.status_code = 500
    client.metrics.flush()
    collection(native[0], monkeypatch, duration_ms=7)
    session.post.return_value.status_code = 200
    client.metrics.flush()
    point = exported_metrics(session)[NAME]["histogram"]["dataPoints"][0]
    assert (point["count"], point["sum"], point["min"], point["max"]) == (2, 9, 2, 7)
    assert sum(point["bucketCounts"]) == 2


def test_generations_are_distinct_and_hooks_are_deferred(
    runtime_client, native, monkeypatch
):
    hook = mock.Mock(side_effect=lambda sample: sample)
    client, session = runtime_client(
        metrics={"capture_runtime_metrics": True, "before_send": hook}
    )
    for generation in range(3):
        collection(native[0], monkeypatch, generation, generation + 1)
    assert not hook.called
    client.metrics.flush()
    points = exported_metrics(session)[NAME]["histogram"]["dataPoints"]
    assert {attributes(p)["cpython.gc.generation"]: p["sum"] for p in points} == {
        0: 1,
        1: 2,
        2: 3,
    }
    assert all(p["count"] == 1 for p in points)
    assert sum(call.args[0]["name"] == NAME for call in hook.call_args_list) == 3


def test_reset_drops_pending_and_inflight_collections(
    runtime_client, native, monkeypatch
):
    client, session = runtime_client()
    module = native[0]
    collection(module, monkeypatch)
    callback = module.gc_callbacks[0]
    callback("start", {"generation": 1})
    client.metrics.reset()
    callback("stop", {"generation": 1})
    client.metrics.flush()
    assert NAME not in exported_metrics(session)
    collection(module, monkeypatch, duration_ms=4)
    client.metrics.flush()
    assert exported_metrics(session)[NAME]["histogram"]["dataPoints"][0]["sum"] == 4


def test_buffer_is_bounded_and_recovers_with_one_warning(
    runtime_client, native, monkeypatch, caplog
):
    module = native[0]
    monkeypatch.setattr(module, "_MAX_GC_DURATION_SAMPLES", 2)
    client, session = runtime_client()
    for _ in range(4):
        collection(module, monkeypatch)
    assert len(client.metrics._runtime_metrics._gc_duration._pending) == 2
    client.metrics.flush()
    assert exported_metrics(session)[NAME]["histogram"]["dataPoints"][0]["count"] == 2
    for _ in range(3):
        collection(module, monkeypatch)
    client.metrics.flush()
    assert caplog.text.count("GC duration observations skipped") == 1
    collection(module, monkeypatch)
    client.metrics.flush()
    assert exported_metrics(session)[NAME]["histogram"]["dataPoints"][0]["count"] == 1


def test_clients_have_independent_callbacks(runtime_client, native, monkeypatch):
    first, _ = runtime_client()
    second, session = runtime_client()
    assert len(native[0].gc_callbacks) == 2
    first.shutdown()
    assert len(native[0].gc_callbacks) == 1
    collection(native[0], monkeypatch)
    second.metrics.flush()
    assert exported_metrics(session)[NAME]["histogram"]["dataPoints"][0]["count"] == 1
    second.shutdown()
    assert native[0].gc_callbacks == []


def test_collector_garbage_collection_removes_callback(native):
    collector = native[0]._MacOSProcessMetrics()
    reference = weakref.ref(collector)
    assert len(native[0].gc_callbacks) == 1
    del collector
    gc.collect()
    assert reference() is None
    assert native[0].gc_callbacks == []


@pytest.mark.parametrize("phase,generation", [("other", 0), ("start", -1), ("stop", 3)])
def test_invalid_gc_notifications_are_ignored(
    runtime_client, native, phase, generation
):
    client, session = runtime_client()
    callback = native[0].gc_callbacks[0]
    callback(phase, {"generation": generation})
    callback("stop", {"generation": 0})
    client.metrics.flush()
    assert NAME not in exported_metrics(session)


def test_clock_failure_and_backwards_clock_do_not_escape(
    runtime_client, native, monkeypatch
):
    client, session = runtime_client()
    clock = mock.Mock(side_effect=[RuntimeError("clock unavailable"), 100, 90])
    monkeypatch.setattr(native[0], "monotonic_ns", clock)
    callback = native[0].gc_callbacks[0]
    callback("start", {"generation": 0})
    callback("start", {"generation": 0})
    callback("stop", {"generation": 0})
    client.metrics.flush()
    assert NAME not in exported_metrics(session)
    collection(native[0], monkeypatch)
    client.metrics.flush()
    assert NAME in exported_metrics(session)


def test_gc_durations_do_not_inherit_flush_span_exemplars(
    runtime_client, native, monkeypatch
):
    client, session = runtime_client()
    monkeypatch.setattr(
        metrics_capture, "_active_trace_context", lambda _: ("a" * 32, "b" * 16)
    )
    collection(native[0], monkeypatch)
    client.metrics.flush()
    assert (
        "exemplars" not in exported_metrics(session)[NAME]["histogram"]["dataPoints"][0]
    )


def test_non_cpython_does_not_install_gc_callback(native, monkeypatch):
    module = native[0]
    monkeypatch.setattr(
        module, "sys", SimpleNamespace(implementation=SimpleNamespace(name="pypy"))
    )
    collector = module._MacOSProcessMetrics()
    assert module.gc_callbacks == []
    assert all(sample.name != NAME for sample in collector.collect())
    collector.close()


def test_histogram_windows_advance_after_each_drain(
    runtime_client, native, monkeypatch
):
    wall_time = mock.Mock(return_value=1000)
    monkeypatch.setattr(native[0], "time", wall_time)
    client, session = runtime_client()
    collection(native[0], monkeypatch)
    wall_time.return_value = 2000
    client.metrics.flush()
    assert (
        exported_metrics(session)[NAME]["histogram"]["dataPoints"][0][
            "startTimeUnixNano"
        ]
        == "1000000000000"
    )
    collection(native[0], monkeypatch)
    wall_time.return_value = 3000
    client.metrics.flush()
    assert (
        exported_metrics(session)[NAME]["histogram"]["dataPoints"][0][
            "startTimeUnixNano"
        ]
        == "2000000000000"
    )


@pytest.mark.skipif(
    sys.platform != "darwin" or sys.implementation.name != "cpython",
    reason="requires macOS CPython",
)
def test_native_gc_callback_records_actual_collection():
    from .._runtime_metrics import _MacOSProcessMetrics

    original_callbacks = list(gc.callbacks)
    was_enabled = gc.isenabled()
    gc.disable()
    collector = _MacOSProcessMetrics()
    try:
        gc.collect(2)
        durations = [sample for sample in collector.collect() if sample.name == NAME]
        assert len(durations) == 1
        assert durations[0].value > 0
        assert durations[0].unit == "ms"
        assert durations[0].attributes == {"cpython.gc.generation": 2}
    finally:
        collector.close()
        if was_enabled:
            gc.enable()
    assert gc.callbacks == original_callbacks


def test_runtime_replacement_removes_previous_callback(runtime_client, native):
    client, _ = runtime_client()
    original = native[0].gc_callbacks[0]
    client.metrics._start_runtime_metrics()
    assert len(native[0].gc_callbacks) == 1
    assert native[0].gc_callbacks[0] is not original
    original("start", {"generation": 0})
    original("stop", {"generation": 0})
    assert not client.metrics._runtime_metrics._gc_duration._pending


def test_shutdown_during_initialization_cleans_callback(
    runtime_client, native, monkeypatch
):
    client, _ = runtime_client(metrics={})
    constructor = native[0]._MacOSProcessMetrics

    def initialize():
        collector = constructor()
        client.shutdown()
        return collector

    monkeypatch.setattr(native[0], "_MacOSProcessMetrics", initialize)
    client.metrics._capture_runtime_metrics = True
    client.metrics._start_runtime_metrics()
    assert client.metrics._runtime_metrics is None
    assert native[0].gc_callbacks == []


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_fork_replaces_callback_and_drops_parent_durations(
    runtime_client, native, monkeypatch
):
    client, session = runtime_client()
    collection(native[0], monkeypatch, duration_ms=50)
    with client.metrics._runtime_metrics._gc_duration._lock:
        pid = os.fork()
    if pid == 0:
        try:
            assert len(native[0].gc_callbacks) == 1
            collection(native[0], monkeypatch, duration_ms=3)
            client.metrics.flush()
            point = exported_metrics(session)[NAME]["histogram"]["dataPoints"][0]
            assert (point["count"], point["sum"]) == (1, 3)
            assert attributes(point)["process.pid"] == os.getpid()
            client.shutdown()
            assert native[0].gc_callbacks == []
            os._exit(0)
        except BaseException:
            os._exit(1)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert len(native[0].gc_callbacks) == 1
    client.metrics.flush()
    assert exported_metrics(session)[NAME]["histogram"]["dataPoints"][0]["sum"] == 50
