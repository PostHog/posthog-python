import ctypes
import os
import sys
import time

import pytest
from google.protobuf.json_format import ParseDict
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)

from . import test_host_metrics as host_tests
from .test_metrics import sent_payload
from .test_runtime_metrics import attributes, exported_metrics

native = host_tests.native
host_native = host_tests.host_native
runtime_client = host_tests.runtime_client

NAME = "system.cpu.utilization"


def core_samples(collector):
    return [sample for sample in collector.collect() if sample.name == NAME]


def advance(state):
    state.core_ticks[0] = [130, 220, 300, 450]
    state.core_ticks[1] = [1000, 2000, 3100, 4000]


def test_per_core_native_function_signatures(host_native):
    module, _, library = host_native
    module._MacOSHostMetrics()
    assert library.host_processor_info.argtypes == [
        ctypes.c_uint32,
        ctypes.c_int32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_int32)),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    assert library.mach_vm_deallocate.argtypes == [
        ctypes.c_uint32,
        ctypes.c_uint64,
        ctypes.c_uint64,
    ]
    assert (
        library.host_processor_info.restype
        == library.mach_vm_deallocate.restype
        == ctypes.c_int32
    )


def test_per_core_modes_without_custom_aggregate(host_native):
    module, state, library = host_native
    collector = module._MacOSHostMetrics()
    advance(state)
    samples = collector.collect()
    cores = [sample for sample in samples if sample.name == NAME]
    assert len(cores) == 8
    values = {
        (
            sample.attributes["cpu.logical_number"],
            sample.attributes["cpu.mode"],
        ): sample.value
        for sample in cores
    }
    assert values == {
        (0, "user"): 0.3,
        (0, "system"): 0.2,
        (0, "idle"): 0,
        (0, "nice"): 0.5,
        (1, "user"): 0,
        (1, "system"): 0,
        (1, "idle"): 1,
        (1, "nice"): 0,
    }
    assert all(sample.unit == "1" and sample.metric_type == "gauge" for sample in cores)
    assert not any(sample.name == "app.system.cpu.utilization" for sample in samples)
    assert not state.allocations
    assert (
        library.host_processor_info.call_count
        == library.mach_vm_deallocate.call_count
        == 2
    )
    assert library.mach_host_self.call_count == library.mach_port_deallocate.call_count


def test_per_core_omits_initial_and_unchanged_counters(host_native):
    module, state, library = host_native
    collector = module._MacOSHostMetrics()
    assert library.host_processor_info.call_count == 1
    assert core_samples(collector) == []
    state.core_ticks[1][0] += 100
    samples = core_samples(collector)
    assert len(samples) == 4
    assert all(sample.attributes["cpu.logical_number"] == 1 for sample in samples)
    assert sum(sample.value for sample in samples) == 1
    assert core_samples(collector) == []
    assert not state.allocations


@pytest.mark.parametrize("before", [2**31 - 10, 2**32 - 10])
def test_per_core_ticks_are_unsigned_and_wrap_without_spikes(host_native, before):
    module, state, _ = host_native
    state.core_ticks = [[before] * 4]
    collector = module._MacOSHostMetrics()
    state.core_ticks = [[(before + delta) % 2**32 for delta in (20, 20, 60, 0)]]
    samples = core_samples(collector)
    assert {sample.attributes["cpu.mode"]: sample.value for sample in samples} == {
        "user": 0.2,
        "system": 0.2,
        "idle": 0.6,
        "nice": 0,
    }


def test_topology_change_rebaselines_all_logical_cpus(host_native):
    module, state, _ = host_native
    collector = module._MacOSHostMetrics()
    advance(state)
    state.core_ticks.append([10, 20, 30, 40])
    assert core_samples(collector) == []
    for ticks in state.core_ticks:
        ticks[0] += 10
    assert len(core_samples(collector)) == 12
    state.core_ticks.pop()
    assert core_samples(collector) == []
    for ticks in state.core_ticks:
        ticks[2] += 10
    samples = core_samples(collector)
    assert len(samples) == 8
    assert all(
        sample.value == (1 if sample.attributes["cpu.mode"] == "idle" else 0)
        for sample in samples
    )


@pytest.mark.parametrize(
    "field,bad,good",
    [
        ("processor_result", 5, 0),
        ("processor_count", 0, None),
        ("processor_info_count", 7, None),
        ("processor_info_count", 9, None),
        ("processor_null", True, False),
        ("deallocate_result", 5, 0),
    ],
)
def test_per_core_failure_is_isolated_frees_resources_and_recovers(
    host_native, caplog, field, bad, good
):
    module, state, library = host_native
    collector = module._MacOSHostMetrics()
    advance(state)
    setattr(state, field, bad)
    for _ in range(2):
        samples = collector.collect()
        assert not any(sample.name == NAME for sample in samples)
        assert any(sample.name == "app.system.memory.pressure" for sample in samples)
        assert not state.allocations
    assert caplog.text.count("Unable to collect host per-CPU metrics") == 1
    setattr(state, field, good)
    assert len(core_samples(collector)) == 8
    assert library.mach_host_self.call_count == library.mach_port_deallocate.call_count
    assert (
        library.mach_vm_deallocate.call_count
        == library.host_processor_info.call_count
        - (2 if field == "processor_null" else 0)
    )


def test_initial_per_core_failure_seeds_on_first_success(host_native):
    module, state, _ = host_native
    state.processor_result = 5
    collector = module._MacOSHostMetrics()
    state.processor_result = 0
    advance(state)
    assert core_samples(collector) == []
    state.core_ticks[0][0] += 10
    assert len(core_samples(collector)) == 4


def test_otlp_per_core_gauges_keep_host_identity_and_latest_reading(
    runtime_client, host_native
):
    client, session = runtime_client(metrics={"capture_host_metrics": True})
    state = host_native[1]
    advance(state)
    session.post.return_value.status_code = 503
    client.metrics.flush()
    first = exported_metrics(session)[NAME]
    assert len(first["gauge"]["dataPoints"]) == 8
    state.core_ticks[0][2] += 100
    state.core_ticks[1][0] += 100
    session.post.return_value.status_code = 200
    client.metrics.flush()
    points = exported_metrics(session)[NAME]["gauge"]["dataPoints"]
    assert len(points) == 8
    for point in points:
        tags = attributes(point)
        assert set(tags) == {"host.name", "cpu.logical_number", "cpu.mode"}
        assert tags["host.name"] == "test-host"
        assert type(tags["cpu.logical_number"]) is int
        expected = (
            1
            if (tags["cpu.logical_number"], tags["cpu.mode"])
            in ((0, "idle"), (1, "user"))
            else 0
        )
        assert point["asDouble"] == expected
    ParseDict(sent_payload(session), ExportMetricsServiceRequest())
    client.shutdown()
    assert not state.allocations
    assert client.metrics._flush_timer is None


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS")
def test_native_per_core_cpu_readings():
    from .._runtime_metrics import _MacOSHostMetrics

    collector = _MacOSHostMetrics()
    start = time.monotonic()
    while time.monotonic() - start < 0.1:
        sum(i * i for i in range(1000))
    samples = core_samples(collector)
    assert samples
    cores = {}
    for sample in samples:
        cpu = sample.attributes["cpu.logical_number"]
        assert 0 <= cpu < os.cpu_count()
        assert sample.attributes["cpu.mode"] in ("user", "system", "idle", "nice")
        assert 0 <= sample.value <= 1
        cores.setdefault(cpu, []).append(sample.value)
    assert all(
        len(values) == 4 and sum(values) == pytest.approx(1)
        for values in cores.values()
    )
