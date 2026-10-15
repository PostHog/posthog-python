import ctypes
import errno
import os
import signal
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


def test_host_opt_in_collects_without_process_metrics(
    runtime_client, native, monkeypatch
):
    module = native[0]
    collector = SimpleNamespace(
        attributes={"host.name": "test-host"},
        start_time_ms=1234,
        collect=mock.Mock(
            return_value=[
                module._RuntimeMetric("app.system.cpu.utilization", 0.25, "1", "gauge")
            ]
        ),
    )
    constructor = mock.Mock(return_value=collector)
    monkeypatch.setattr(module, "_MacOSHostMetrics", constructor, raising=False)
    client, session = runtime_client(metrics={"capture_host_metrics": True})
    assert client._metrics is not None
    assert client.metrics._runtime_metrics is None
    client.metrics._flush_timer.fire()
    constructor.assert_called_once()
    metric = exported_metrics(session)["app.system.cpu.utilization"]
    point = metric["gauge"]["dataPoints"][0]
    assert point["asDouble"] == 0.25
    assert attributes(point) == {"host.name": "test-host"}
    assert client.metrics._flush_timer.active
    client.shutdown()
    assert client.metrics._host_metrics is None
    assert client.metrics._flush_timer is None


@pytest.fixture
def host_native(native, monkeypatch):
    module = native[0]
    state = SimpleNamespace(
        ticks=[1000, 500, 2000, 100],
        free=100,
        inactive=50,
        speculative=20,
        page_size=16384,
        swapins=123,
        swapouts=456,
        swap_used=98765,
        pressure=1,
        cpu_result=0,
        vm_result=0,
        page_result=0,
        cpu_count=4,
        vm_count=38,
        pressure_error=0,
        swap_error=0,
        pressure_size=4,
        swap_size=32,
    )

    def statistics(host, flavor, output, count):
        assert host == 42
        size = ctypes.cast(count, ctypes.POINTER(ctypes.c_uint32)).contents
        if flavor == 3:
            assert size.value == 4
            info = ctypes.cast(output, ctypes.POINTER(module._HostCPULoadInfo)).contents
            info.cpu_ticks[:] = state.ticks
            size.value = state.cpu_count
            return state.cpu_result
        assert flavor == 4 and size.value == 38
        info = ctypes.cast(output, ctypes.POINTER(module._VMStatistics64)).contents
        info.free_count = state.free
        info.inactive_count = state.inactive
        info.speculative_count = state.speculative
        info.pageins = 1000000
        info.pageouts = 2000000
        info.swapins = state.swapins
        info.swapouts = state.swapouts
        size.value = state.vm_count
        return state.vm_result

    def page_size(host, output):
        assert host == 42
        ctypes.cast(
            output, ctypes.POINTER(ctypes.c_size_t)
        ).contents.value = state.page_size
        return state.page_result

    def sysctl(name, output, size_pointer, new_value, new_size):
        assert new_value is None and new_size == 0
        size = ctypes.cast(size_pointer, ctypes.POINTER(ctypes.c_size_t)).contents
        if name == b"vm.swapusage":
            assert size.value == 32
            info = ctypes.cast(output, ctypes.POINTER(module._SwapUsage)).contents
            info.used = state.swap_used
            size.value = state.swap_size
            error = state.swap_error
        else:
            assert name == b"kern.memorystatus_vm_pressure_level"
            assert size.value == 4
            ctypes.cast(
                output, ctypes.POINTER(ctypes.c_uint32)
            ).contents.value = state.pressure
            size.value = state.pressure_size
            error = state.pressure_error
        ctypes.set_errno(error)
        return -1 if error else 0

    library = mock.Mock(
        mach_host_self=mock.Mock(return_value=42),
        mach_task_self=mock.Mock(return_value=43),
        mach_port_deallocate=mock.Mock(return_value=0),
        host_statistics=mock.Mock(side_effect=statistics),
        host_statistics64=mock.Mock(side_effect=statistics),
        host_page_size=mock.Mock(side_effect=page_size),
        sysctlbyname=mock.Mock(side_effect=sysctl),
    )
    original = module.ctypes.CDLL

    def load(path, **kwargs):
        if path == "/usr/lib/libSystem.B.dylib":
            return library
        return original(path, **kwargs)

    monkeypatch.setattr(module.ctypes, "CDLL", load)
    monkeypatch.setattr(module, "gethostname", lambda: "test-host")
    return module, state, library


def host_values(collector):
    return {sample.name: sample.value for sample in collector.collect()}


def test_native_host_abi(host_native):
    module = host_native[0]
    assert ctypes.sizeof(module._HostCPULoadInfo) == 16
    assert ctypes.sizeof(module._VMStatistics64) == 152
    assert module._VMStatistics64.swapins.offset == 112
    assert module._VMStatistics64.total_uncompressed_pages_in_compressor.offset == 144
    assert ctypes.sizeof(module._SwapUsage) == 32
    assert module._SwapUsage.encrypted.offset == 28


@pytest.mark.parametrize("page_size", [4096, 16384])
def test_native_host_values_and_cumulative_baselines(host_native, page_size):
    module, state, library = host_native
    state.page_size = page_size
    collector = module._MacOSHostMetrics()
    state.ticks = [1050, 525, 2025, 100]
    state.swapins += 3
    state.swapouts += 5
    samples = collector.collect()
    values = {sample.name: sample.value for sample in samples}
    assert values["app.system.cpu.utilization"] == 0.75
    assert values["app.system.memory.available.estimated"] == 150 * page_size
    assert values["app.system.memory.pressure"] == 1
    assert values["system.paging.usage"] == 98765
    swap = [sample for sample in samples if sample.name == "app.system.swap.io"]
    assert [(sample.attributes, sample.value) for sample in swap] == [
        ({"system.paging.direction": "in"}, 3 * page_size),
        ({"system.paging.direction": "out"}, 5 * page_size),
    ]
    assert all(
        sample.unit == "By" and sample.metric_type == "observable_counter"
        for sample in swap
    )
    assert collector.attributes == {"host.name": "test-host"}
    assert library.mach_port_deallocate.call_args_list == [mock.call(43, 42)] * 4


def test_host_cpu_omits_initial_and_zero_samples_and_handles_wrap(host_native):
    module, state, _ = host_native
    state.ticks = [2**32 - 10] * 4
    collector = module._MacOSHostMetrics()
    assert "app.system.cpu.utilization" not in host_values(collector)
    state.ticks = [10, 10, 50, 10]
    assert host_values(collector)["app.system.cpu.utilization"] == 0.5
    assert "app.system.cpu.utilization" not in host_values(collector)


@pytest.mark.parametrize("level", [1, 2, 4])
def test_host_pressure_uses_native_categorical_levels(host_native, level):
    module, state, _ = host_native
    state.pressure = level
    collector = module._MacOSHostMetrics()
    sample = next(
        sample
        for sample in collector.collect()
        if sample.name == "app.system.memory.pressure"
    )
    assert sample.value == level
    assert sample.metric_type == "gauge"
    assert sample.unit == "{level}"


@pytest.mark.parametrize("level", [0, 3, 8])
def test_unknown_pressure_is_omitted_and_recovers(host_native, caplog, level):
    module, state, _ = host_native
    state.pressure = level
    collector = module._MacOSHostMetrics()
    assert "app.system.memory.pressure" not in host_values(collector)
    assert "app.system.memory.pressure" not in host_values(collector)
    assert caplog.text.count("Unable to collect host memory pressure") == 1
    state.pressure = 2
    assert host_values(collector)["app.system.memory.pressure"] == 2


@pytest.mark.parametrize(
    "field,bad,good,omitted",
    [
        ("cpu_result", 5, 0, {"app.system.cpu.utilization"}),
        ("cpu_count", 3, 4, {"app.system.cpu.utilization"}),
        (
            "vm_result",
            5,
            0,
            {"app.system.memory.available.estimated", "app.system.swap.io"},
        ),
        (
            "vm_count",
            24,
            38,
            {"app.system.memory.available.estimated", "app.system.swap.io"},
        ),
        (
            "page_result",
            5,
            0,
            {"app.system.memory.available.estimated", "app.system.swap.io"},
        ),
        (
            "page_size",
            0,
            16384,
            {"app.system.memory.available.estimated", "app.system.swap.io"},
        ),
        ("pressure_error", errno.EPERM, 0, {"app.system.memory.pressure"}),
        ("pressure_error", errno.ENOENT, 0, {"app.system.memory.pressure"}),
        ("pressure_size", 3, 4, {"app.system.memory.pressure"}),
        ("swap_error", errno.EPERM, 0, {"system.paging.usage"}),
        ("swap_size", 24, 32, {"system.paging.usage"}),
    ],
)
def test_host_source_failures_are_isolated_and_recover(
    host_native, caplog, field, bad, good, omitted
):
    module, state, library = host_native
    collector = module._MacOSHostMetrics()
    setattr(state, field, bad)
    expected = {
        "app.system.cpu.utilization",
        "app.system.memory.available.estimated",
        "app.system.memory.pressure",
        "app.system.swap.io",
        "system.paging.usage",
    }
    for _ in range(2):
        state.ticks[0] += 100
        assert set(host_values(collector)) == expected - omitted
    assert caplog.text.count("Unable to collect host") == 1
    assert library.mach_host_self.call_count == library.mach_port_deallocate.call_count
    setattr(state, field, good)
    state.ticks[0] += 100
    assert set(host_values(collector)) == expected


def test_failed_initial_vm_read_seeds_swap_baseline_on_recovery(host_native):
    module, state, _ = host_native
    state.vm_result = 5
    collector = module._MacOSHostMetrics()
    state.swapins += 100
    state.vm_result = 0
    samples = collector.collect()
    assert [
        sample.value for sample in samples if sample.name == "app.system.swap.io"
    ] == [0, 0]
    state.swapins += 2
    samples = collector.collect()
    assert [
        sample.value for sample in samples if sample.name == "app.system.swap.io"
    ] == [32768, 0]


def test_host_and_process_exports_keep_separate_identity_and_retry_values(
    runtime_client, host_native, native
):
    client, session = runtime_client(
        metrics={"capture_runtime_metrics": True, "capture_host_metrics": True}
    )
    native[1].time += 2
    native[1].user += 1
    host_native[1].ticks[0] += 100
    host_native[1].swapins += 2
    session.post.return_value.status_code = 503
    client.metrics._consecutive_send_failures = 1
    client.metrics._flush_timer.fire()
    assert client.metrics._flush_timer.interval == 20
    first = exported_metrics(session)["app.system.swap.io"]["sum"]["dataPoints"]
    host_native[1].swapins += 3
    host_native[1].swap_used = 0
    session.post.return_value.status_code = 200
    client.metrics.flush()
    metrics = exported_metrics(session)
    ParseDict(sent_payload(session), ExportMetricsServiceRequest())
    assert len(metrics) == 15
    for name, metric in metrics.items():
        for point in metric.get("sum", metric.get("gauge"))["dataPoints"]:
            tags = attributes(point)
            if name.startswith("app.system.") or name == "system.paging.usage":
                assert tags["host.name"] == "test-host"
                assert "process.pid" not in tags and "process.runtime_id" not in tags
            else:
                assert tags["process.pid"] == os.getpid()
                assert "host.name" not in tags
    swap = metrics["app.system.swap.io"]["sum"]
    assert swap["aggregationTemporality"] == 2 and swap["isMonotonic"]
    inbound = next(
        point
        for point in swap["dataPoints"]
        if attributes(point)["system.paging.direction"] == "in"
    )
    assert inbound["asDouble"] == 5 * 16384
    assert inbound["startTimeUnixNano"] == first[0]["startTimeUnixNano"]
    usage = metrics["system.paging.usage"]["sum"]
    assert usage["aggregationTemporality"] == 2 and not usage["isMonotonic"]
    assert usage["dataPoints"][0]["asDouble"] == 0


@pytest.mark.parametrize("flag", [False, None, 1, "true", {}, []])
def test_host_opt_in_requires_exact_true(runtime_client, native, monkeypatch, flag):
    constructor = mock.Mock(side_effect=AssertionError("unexpected host collection"))
    monkeypatch.setattr(native[0], "_MacOSHostMetrics", constructor)
    client, _ = runtime_client(metrics={"capture_host_metrics": flag})
    assert client.metrics._host_metrics is None
    constructor.assert_not_called()


@pytest.mark.parametrize("options", [{"disabled": True}, {"send": False}])
def test_host_collection_skips_non_sending_clients(
    runtime_client, host_native, options
):
    client, session = runtime_client(metrics={"capture_host_metrics": True}, **options)
    assert client.metrics._host_metrics is None
    assert client.metrics._flush_timer is None
    host_native[2].mach_host_self.assert_not_called()
    host_native[2].sysctlbyname.assert_not_called()
    session.post.assert_not_called()


def test_host_collection_warns_on_unsupported_platform(
    runtime_client, monkeypatch, caplog
):
    monkeypatch.setattr(metrics_capture.sys, "platform", "linux")
    client, _ = runtime_client(metrics={"capture_host_metrics": True})
    assert client.metrics._host_metrics is None
    assert "Automatic host metrics are currently supported only on macOS" in caplog.text


@pytest.mark.parametrize("option", ["capture_host_metrics", "capture_runtime_metrics"])
def test_optional_native_import_failure_preserves_manual_config(runtime_client, option):
    original_import = __import__

    def load(name, *args, **kwargs):
        if name == "_runtime_metrics":
            raise ImportError("native module unavailable")
        return original_import(name, *args, **kwargs)

    hook = mock.Mock(side_effect=lambda sample: sample)
    with mock.patch("builtins.__import__", side_effect=load):
        client, session = runtime_client(
            metrics={option: True, "service_name": "configured", "before_send": hook}
        )
    client.metrics.count("manual")
    client.metrics.flush()
    assert (
        attributes(sent_payload(session)["resourceMetrics"][0]["resource"])[
            "service.name"
        ]
        == "configured"
    )
    hook.assert_called_once()
    assert client.metrics._host_metrics is None
    assert client.metrics._runtime_metrics is None


@pytest.mark.parametrize("failed", ["_MacOSHostMetrics", "_MacOSProcessMetrics"])
def test_collector_initialization_failures_do_not_disable_other_collector(
    runtime_client, host_native, monkeypatch, failed
):
    monkeypatch.setattr(
        host_native[0], failed, mock.Mock(side_effect=OSError("unavailable"))
    )
    client, session = runtime_client(
        metrics={"capture_host_metrics": True, "capture_runtime_metrics": True}
    )
    client.metrics.flush()
    names = exported_metrics(session)
    assert ("process.cpu.time" in names) == (failed != "_MacOSProcessMetrics")
    assert ("app.system.memory.available.estimated" in names) == (
        failed != "_MacOSHostMetrics"
    )


def test_shutdown_during_host_initialization_does_not_install_collector(
    runtime_client, host_native, monkeypatch
):
    client, session = runtime_client(metrics=None)
    client._metrics_config = {"capture_host_metrics": True}
    constructor = host_native[0]._MacOSHostMetrics

    def initialize():
        client.shutdown()
        return constructor()

    monkeypatch.setattr(host_native[0], "_MacOSHostMetrics", initialize)
    client._start_runtime_metrics()
    assert client._shutdown_complete
    assert client.metrics._host_metrics is None
    assert client.metrics._flush_timer is None
    session.post.assert_not_called()


def test_host_before_send_can_shutdown_and_reset_keeps_collection(
    runtime_client, host_native
):
    closed = False

    def hook(sample):
        if closed:
            client.shutdown()
        return sample

    client, session = runtime_client(
        metrics={"capture_host_metrics": True, "before_send": hook}
    )
    first = client.metrics._flush_timer
    client.metrics.reset()
    assert not first.active and client.metrics._flush_timer.active
    closed = True
    client.metrics.flush()
    client.shutdown()
    assert client._shutdown_complete
    assert client.metrics._host_metrics is None
    assert client.metrics._flush_timer is None
    assert not client.metrics._series
    assert "app.system.memory.pressure" in exported_metrics(session)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_fork_in_process_hook_skips_inherited_host_sampler(
    runtime_client, host_native, monkeypatch
):
    parent = os.getpid()
    children = []

    def hook(sample):
        if not children:
            children.append(os.fork())
            if os.getpid() != parent:
                signal.alarm(5)
        return sample

    client, _ = runtime_client(
        metrics={
            "capture_runtime_metrics": True,
            "capture_host_metrics": True,
            "before_send": hook,
        }
    )
    old_host = client.metrics._host_metrics
    read = mock.Mock(wraps=old_host.collect)
    monkeypatch.setattr(old_host, "collect", read)
    try:
        client.metrics.flush()
        if os.getpid() != parent:
            read.assert_not_called()
            assert client.metrics._host_metrics is None
    except BaseException:
        if os.getpid() != parent:
            os._exit(1)
        raise
    if os.getpid() != parent:
        os._exit(0)
    _, status = os.waitpid(children[0], 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    read.assert_called_once()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
@pytest.mark.parametrize("runtime", [False, True])
def test_fork_does_not_duplicate_host_collection(runtime_client, host_native, runtime):
    client, session = runtime_client(
        metrics={"capture_host_metrics": True, "capture_runtime_metrics": runtime}
    )
    parent_host = client.metrics._host_metrics
    pid = os.fork()
    if pid == 0:
        signal.alarm(5)
        try:
            assert client.metrics._host_metrics is None
            assert (client.metrics._runtime_metrics is not None) == runtime
            assert (client.metrics._flush_timer is not None) == runtime
            client.metrics.flush()
            if runtime:
                assert "app.system.swap.io" not in exported_metrics(session)
            else:
                session.post.assert_not_called()
            explicit, _ = runtime_client(metrics={"capture_host_metrics": True})
            assert explicit.metrics._host_metrics is not None
            explicit.shutdown()
        except BaseException:
            os._exit(1)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert client.metrics._host_metrics is parent_host
