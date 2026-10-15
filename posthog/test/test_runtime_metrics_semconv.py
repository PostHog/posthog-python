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


def points(metric):
    return metric.get("sum", metric.get("gauge"))["dataPoints"]


def by_attribute(metric, key):
    return {attributes(point)[key]: point for point in points(metric)}


def test_otel_types_units_cpu_modes_gc_generations_and_identity(runtime_client, native):
    client, session = runtime_client()
    state = native[1]
    state.time += 2
    state.user += 2
    state.system += 1
    state.switches += 9
    for generation, stats in enumerate(state.gc_stats):
        for factor, field in enumerate(
            ("collections", "collected", "uncollectable"), 1
        ):
            stats[field] += (generation + 1) * factor
    client.metrics.flush()
    metrics = exported_metrics(session)
    expected = {
        "process.cpu.time": ("s", True),
        "process.cpu.utilization": ("1", None),
        "process.memory.usage": ("By", False),
        "process.memory.utilization": ("1", None),
        "process.thread.count": ("{thread}", False),
        "cpython.gc.collections": ("{collection}", True),
        "cpython.gc.collected_objects": ("{object}", True),
        "cpython.gc.uncollectable_objects": ("{object}", True),
        "app.process.cpu.percent": ("%", None),
        "app.process.cpu.ctx_switch.total": ("{context_switch}", True),
    }
    assert set(metrics) == set(expected)
    identity = client.metrics._runtime_metrics.attributes
    for name, (unit, monotonic) in expected.items():
        metric = metrics[name]
        assert metric["unit"] == unit
        if monotonic is None:
            assert "gauge" in metric
        else:
            assert metric["sum"]["isMonotonic"] is monotonic
            assert metric["sum"]["aggregationTemporality"] == 2
        for point in points(metric):
            assert attributes(point).items() >= identity.items()
            if monotonic is not None:
                assert int(point["startTimeUnixNano"]) <= int(point["timeUnixNano"])
    assert sum(len(points(metric)) for metric in metrics.values()) == 18
    cpu_time = by_attribute(metrics["process.cpu.time"], "cpu.mode")
    assert set(cpu_time) == {"user", "system"}
    assert cpu_time["user"]["asDouble"] == 2
    assert cpu_time["system"]["asDouble"] == 1
    utilization = by_attribute(metrics["process.cpu.utilization"], "cpu.mode")
    assert set(utilization) == {"user", "system"}
    assert utilization["user"]["asDouble"] == 0.125
    assert utilization["system"]["asDouble"] == 0.0625
    assert points(metrics["app.process.cpu.percent"])[0]["asDouble"] == 150
    assert points(metrics["process.memory.usage"])[0]["asDouble"] == state.rss
    assert points(metrics["process.thread.count"])[0]["asDouble"] == state.threads
    assert points(metrics["app.process.cpu.ctx_switch.total"])[0]["asDouble"] == 9
    for name, factor in (
        ("cpython.gc.collections", 1),
        ("cpython.gc.collected_objects", 2),
        ("cpython.gc.uncollectable_objects", 3),
    ):
        generations = by_attribute(metrics[name], "cpython.gc.generation")
        assert set(generations) == {0, 1, 2}
        for generation in range(3):
            assert generations[generation]["asDouble"] == (generation + 1) * factor
    decoded = ParseDict(sent_payload(session), ExportMetricsServiceRequest())
    assert len(decoded.resource_metrics[0].scope_metrics[0].metrics) == 10


@pytest.mark.parametrize(
    "name",
    [
        "process.cpu.time",
        "cpython.gc.collections",
        "cpython.gc.collected_objects",
        "cpython.gc.uncollectable_objects",
        "app.process.cpu.ctx_switch.total",
    ],
)
def test_cumulative_counters_exclude_work_before_collection_starts(
    runtime_client, name
):
    client, session = runtime_client()
    client.metrics.flush()
    metric = exported_metrics(session)[name]
    assert all(point["asDouble"] == 0 for point in points(metric))
    assert all(
        int(point["startTimeUnixNano"])
        == client.metrics._runtime_metrics.start_time_ms * 1_000_000
        for point in points(metric)
    )


def test_memory_utilization_tracks_rss_and_physical_memory(runtime_client, native):
    client, session = runtime_client()
    state = native[1]
    for rss, physical_pages in ((128, 1024), (256, 1024), (256, 2048), (0, 2048)):
        state.rss = rss
        state.physical_pages = physical_pages
        state.time += 10
        client.metrics.flush()
        metrics = exported_metrics(session)
        utilization = metrics["process.memory.utilization"]
        assert utilization["unit"] == "1"
        assert points(utilization)[0]["asDouble"] == rss / (
            physical_pages * state.page_size
        )
        assert "gauge" in utilization
        assert points(metrics["process.memory.usage"])[0]["asDouble"] == rss


@pytest.mark.parametrize("key", ["SC_PHYS_PAGES", "SC_PAGE_SIZE"])
@pytest.mark.parametrize(
    "unavailable", [-1, 0, OSError("unavailable"), ValueError("unknown")]
)
def test_unavailable_physical_memory_omits_utilization_and_recovers(
    runtime_client, native, monkeypatch, key, unavailable
):
    module, state, _ = native
    original_sysconf = module.os.sysconf

    def sysconf(name):
        if name == key:
            if isinstance(unavailable, Exception):
                raise unavailable
            return unavailable
        return original_sysconf(name)

    monkeypatch.setattr(module.os, "sysconf", sysconf)
    client, session = runtime_client()
    state.time += 10
    client.metrics.flush()
    metrics = exported_metrics(session)
    assert "process.memory.utilization" not in metrics
    assert "process.memory.usage" in metrics
    assert "process.thread.count" in metrics
    assert "process.cpu.time" in metrics
    assert "app.process.cpu.ctx_switch.total" in metrics
    monkeypatch.setattr(module.os, "sysconf", original_sysconf)
    state.time += 10
    client.metrics.flush()
    utilization = exported_metrics(session)["process.memory.utilization"]
    assert points(utilization)[0]["asDouble"] == state.rss / (
        state.physical_pages * state.page_size
    )


def test_cpu_utilization_tracks_available_cpus(runtime_client, native):
    client, session = runtime_client()
    state = native[1]
    for cpus in (8, 4):
        state.cpus = cpus
        state.time += 10
        state.user += 10
        client.metrics.flush()
        metrics = exported_metrics(session)
        utilization = by_attribute(metrics["process.cpu.utilization"], "cpu.mode")
        assert utilization["user"]["asDouble"] == 1 / cpus
        assert utilization["system"]["asDouble"] == 0
        assert points(metrics["app.process.cpu.percent"])[0]["asDouble"] == 100


@pytest.mark.parametrize("cpus", [None, 0])
def test_unknown_cpu_availability_omits_normalized_utilization(
    runtime_client, native, cpus
):
    client, session = runtime_client()
    native[1].cpus = cpus
    native[1].time += 10
    native[1].user += 10
    client.metrics.flush()
    metrics = exported_metrics(session)
    assert "process.cpu.utilization" not in metrics
    assert "process.cpu.time" in metrics
    assert points(metrics["app.process.cpu.percent"])[0]["asDouble"] == 100


def test_cpu_availability_falls_back_on_older_python(
    runtime_client, native, monkeypatch
):
    monkeypatch.delattr(native[0].os, "process_cpu_count")
    monkeypatch.setattr(native[0].os, "cpu_count", lambda: 4)
    client, session = runtime_client()
    native[1].time += 10
    native[1].user += 10
    client.metrics.flush()
    utilization = by_attribute(
        exported_metrics(session)["process.cpu.utilization"], "cpu.mode"
    )
    assert utilization["user"]["asDouble"] == 0.25


def test_cumulative_observations_keep_latest_value_and_stable_start(
    runtime_client, native, monkeypatch
):
    client, session = runtime_client()
    state = native[1]
    wall_start = metrics_capture.time.time()
    monkeypatch.setattr(metrics_capture.time, "time", lambda: wall_start + state.time)
    state.time += 10
    state.user += 2
    client.metrics.flush()
    first = exported_metrics(session)
    state.time += 10
    state.user += 0.5
    state.system += 0.25
    state.switches += 2
    state.rss = 100
    state.threads = 3
    state.gc_stats[0]["collections"] += 1
    client.metrics._collect_runtime_metrics()
    state.time += 10
    client.metrics.flush()
    second = exported_metrics(session)
    cpu = by_attribute(second["process.cpu.time"], "cpu.mode")
    assert cpu["user"]["asDouble"] == 2.5
    assert cpu["system"]["asDouble"] == 0.25
    assert points(second["process.memory.usage"])[0]["asDouble"] == 100
    assert points(second["process.thread.count"])[0]["asDouble"] == 3
    assert points(second["app.process.cpu.ctx_switch.total"])[0]["asDouble"] == 2
    assert (
        by_attribute(second["cpython.gc.collections"], "cpython.gc.generation")[0][
            "asDouble"
        ]
        == 1
    )
    for name, metric in first.items():
        if "sum" in metric:
            assert (
                points(metric)[0]["startTimeUnixNano"]
                == points(second[name])[0]["startTimeUnixNano"]
            )


def test_retry_merge_preserves_newer_cumulative_observations(runtime_client, native):
    client, session = runtime_client()
    state = native[1]
    state.time += 10
    state.user += 2

    def capture_during_failed_send(*args, **kwargs):
        state.time += 10
        state.user += 0.5
        state.rss = 100
        state.threads = 3
        state.gc_stats[0]["collected"] += 20
        state.switches += 7
        client.metrics._collect_runtime_metrics()
        return mock.Mock(status_code=500)

    session.post.side_effect = capture_during_failed_send
    client.metrics.flush()
    first = exported_metrics(session)
    session.post.side_effect = None
    session.post.return_value.status_code = 200
    state.time += 10
    client.metrics.flush()
    metrics = exported_metrics(session)
    assert (
        by_attribute(metrics["process.cpu.time"], "cpu.mode")["user"]["asDouble"] == 2.5
    )
    assert points(metrics["process.memory.usage"])[0]["asDouble"] == 100
    assert points(metrics["process.thread.count"])[0]["asDouble"] == 3
    assert (
        by_attribute(metrics["cpython.gc.collected_objects"], "cpython.gc.generation")[
            0
        ]["asDouble"]
        == 20
    )
    assert points(metrics["app.process.cpu.ctx_switch.total"])[0]["asDouble"] == 7
    assert (
        points(first["process.cpu.time"])[0]["startTimeUnixNano"]
        == points(metrics["process.cpu.time"])[0]["startTimeUnixNano"]
    )


def test_retry_without_new_reading_preserves_observation_timestamp(
    runtime_client, native, monkeypatch
):
    client, session = runtime_client()
    state = native[1]
    wall_start = metrics_capture.time.time()
    monkeypatch.setattr(metrics_capture.time, "time", lambda: wall_start + state.time)
    state.time += 10
    session.post.return_value.status_code = 500
    client.metrics.flush()
    first = exported_metrics(session)
    monkeypatch.setattr(
        native[0], "getrusage", mock.Mock(side_effect=OSError("unavailable"))
    )
    state.time += 10
    session.post.return_value.status_code = 200
    client.metrics.flush()
    second = exported_metrics(session)
    for name, metric in first.items():
        if "sum" in metric:
            assert points(metric) == points(second[name])


def test_non_cpython_omits_cpython_metrics_only(runtime_client, native, monkeypatch):
    client, session = runtime_client()
    with monkeypatch.context() as patch:
        patch.setattr(native[0].sys.implementation, "name", "pypy")
        native[1].time += 10
        client.metrics.flush()
    metrics = exported_metrics(session)
    assert not any(name.startswith("cpython.") for name in metrics)
    assert "process.cpu.time" in metrics
    assert "process.memory.usage" in metrics


def test_native_failure_preserves_context_switch_counter_across_gap(
    runtime_client, native
):
    client, session = runtime_client()
    state = native[1]
    state.time += 10
    client.metrics.flush()
    state.size = 8
    state.time += 10
    state.switches += 100
    client.metrics.flush()
    metrics = exported_metrics(session)
    assert "process.memory.usage" not in metrics
    assert "process.memory.utilization" not in metrics
    assert "app.process.cpu.ctx_switch.total" not in metrics
    assert "cpython.gc.collections" in metrics
    state.size = 96
    state.time += 10
    state.switches += 2
    client.metrics.flush()
    assert (
        points(exported_metrics(session)["app.process.cpu.ctx_switch.total"])[0][
            "asDouble"
        ]
        == 102
    )
