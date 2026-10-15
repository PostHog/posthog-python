import ctypes
import importlib
import os
import signal
import sys
import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest

import posthog
from posthog import metrics_capture
from posthog.client import Client
from posthog.test.test_metrics import mock_session, sent_payload


class ManualTimer:
    def __init__(self, interval, callback):
        self.interval = interval
        self.callback = callback
        self.daemon = False
        self.active = False
        self.started = False

    def start(self):
        assert not self.started
        self.started = True
        self.active = True

    def cancel(self):
        self.active = False

    def fire(self):
        self.active = False
        self.callback()


@pytest.fixture
def native(monkeypatch):
    pytest.importorskip("resource")
    module = importlib.import_module("posthog._runtime_metrics")
    state = SimpleNamespace(
        user=1.5,
        system=0.5,
        time=100.0,
        rss=123456,
        size=96,
        threads=7,
        switches=100,
        voluntary=50,
        involuntary=100,
        cpus=8,
        physical_pages=4096,
        page_size=4096,
        gc_stats=[
            {"collections": 10, "collected": 25, "uncollectable": 1},
            {"collections": 3, "collected": 18, "uncollectable": 0},
            {"collections": 1, "collected": 5, "uncollectable": 2},
        ],
    )

    def read_process(pid, flavor, arg, pointer, size):
        assert pid == os.getpid()
        assert flavor == 4
        assert arg == 0
        assert size == 96
        info = ctypes.cast(pointer, ctypes.POINTER(module._ProcTaskInfo)).contents
        info.pti_resident_size = state.rss
        info.pti_threadnum = state.threads
        info.pti_csw = state.switches
        return state.size

    proc_pidinfo = mock.Mock(side_effect=read_process)
    library = mock.Mock(proc_pidinfo=proc_pidinfo)
    monkeypatch.setattr(module.ctypes, "CDLL", mock.Mock(return_value=library))
    monkeypatch.setattr(
        module,
        "getrusage",
        lambda who: SimpleNamespace(
            ru_utime=state.user,
            ru_stime=state.system,
            ru_nvcsw=state.voluntary,
            ru_nivcsw=state.involuntary,
        ),
    )
    monkeypatch.setattr(module, "gethostname", lambda: "test-host")
    monkeypatch.setattr(module, "monotonic", lambda: state.time)
    monkeypatch.setattr(module, "gc_callbacks", [], raising=False)
    monkeypatch.setattr(
        module, "get_stats", lambda: [stats.copy() for stats in state.gc_stats]
    )
    monkeypatch.setattr(
        module.os,
        "sysconf",
        lambda name: {
            "SC_PHYS_PAGES": state.physical_pages,
            "SC_PAGE_SIZE": state.page_size,
        }[name],
    )
    monkeypatch.setattr(
        module.os, "process_cpu_count", lambda: state.cpus, raising=False
    )
    return module, state, proc_pidinfo


@pytest.fixture
def runtime_client(native, monkeypatch):
    monkeypatch.setattr(metrics_capture.sys, "platform", "darwin")
    monkeypatch.setattr(metrics_capture.threading, "Timer", ManualTimer)
    session = mock_session()
    monkeypatch.setattr(metrics_capture, "_get_session", lambda: session)
    clients = []

    def create(**kwargs):
        kwargs.setdefault("sync_mode", True)
        kwargs.setdefault(
            "metrics", {"capture_runtime_metrics": True, "service_name": "worker"}
        )
        client = Client("phc_test_key", host="https://us.example.com", **kwargs)
        clients.append(client)
        return client, session

    yield create
    for client in clients:
        client.shutdown()


def exported_metrics(session):
    return {
        metric["name"]: metric
        for group in sent_payload(session)["resourceMetrics"]
        for scope in group["scopeMetrics"]
        for metric in scope["metrics"]
    }


def attributes(point):
    return {
        item["key"]: next(iter(item["value"].values())) for item in point["attributes"]
    }


@pytest.mark.parametrize("before", [2**31 - 2, 2**32 - 2])
def test_context_switch_native_counters_preserve_large_values(native, before):
    module, state, _ = native
    state.involuntary = before
    collector = module._MacOSProcessMetrics()
    state.involuntary += 5
    state.time += 10
    samples = {sample.name: sample.value for sample in collector.collect()}
    assert samples["process.context_switches"] == 5


def test_native_read_failure_omits_native_metrics_and_warns_once(native, caplog):
    module, state, _ = native
    collector = module._MacOSProcessMetrics()
    state.size = 8
    for _ in range(2):
        state.time += 10
        samples = {sample.name: sample.value for sample in collector.collect()}
        assert "process.cpu.time" in samples
        assert "cpython.gc.collections" in samples
        assert "process.memory.usage" not in samples
        assert "process.memory.utilization" not in samples
        assert "process.thread.count" not in samples
        assert "process.context_switches" in samples
    state.time += 10
    state.size = 96
    state.involuntary += 10000
    samples = {sample.name: sample.value for sample in collector.collect()}
    assert samples["process.thread.count"] == 7
    assert samples["process.context_switches"] == 10000
    assert caplog.text.count("Unable to collect native process metrics") == 1


def test_native_baseline_failure_does_not_disable_cpu_and_gc(native):
    module, state, _ = native
    state.size = 8
    collector = module._MacOSProcessMetrics()
    state.time += 10
    samples = {sample.name: sample.value for sample in collector.collect()}
    assert "process.cpu.time" in samples
    assert "cpython.gc.collections" in samples
    state.time += 10
    state.size = 96
    samples = {sample.name: sample.value for sample in collector.collect()}
    assert samples["process.memory.usage"] == state.rss
    assert samples["process.context_switches"] == 0
    state.involuntary += 3
    samples = {sample.name: sample.value for sample in collector.collect()}
    assert samples["process.context_switches"] == 3


def test_opt_in_starts_collection_without_manual_metrics_calls(runtime_client):
    client, _ = runtime_client()
    assert client._metrics is not None
    assert client._metrics._runtime_metrics is not None
    assert client._metrics._flush_timer.active
    assert client._metrics._flush_timer.interval == 10


@pytest.mark.parametrize("metrics", [None, {}, {"capture_runtime_metrics": False}])
def test_disabled_by_default(metrics):
    client = Client("phc_test_key", sync_mode=True, metrics=metrics)
    try:
        assert client._metrics is None
        assert client.metrics._runtime_metrics is None
        assert client.metrics._flush_timer is None
    finally:
        client.shutdown()


@pytest.mark.parametrize("value", ["true", 1, {}, None])
def test_option_requires_true_boolean(runtime_client, value, caplog):
    client, _ = runtime_client(metrics={"capture_runtime_metrics": value})
    assert client.metrics._runtime_metrics is None
    assert "expected a boolean" in caplog.text


@pytest.mark.parametrize("kwargs", [{"disabled": True}, {"send": False}])
def test_disabled_or_non_sending_client_does_not_collect(
    runtime_client, native, kwargs
):
    client, _ = runtime_client(**kwargs)
    assert client.metrics._runtime_metrics is None
    assert client.metrics._flush_timer is None
    native[2].assert_not_called()


def test_unsupported_platform_is_noop(monkeypatch, caplog):
    monkeypatch.setattr(metrics_capture.sys, "platform", "linux")
    client = Client(
        "phc_test_key", sync_mode=True, metrics={"capture_runtime_metrics": True}
    )
    try:
        assert client.metrics._runtime_metrics is None
        assert client.metrics._flush_timer is None
        assert "only on macOS" in caplog.text
    finally:
        client.shutdown()


def test_timer_collects_process_metrics_and_continues(runtime_client, native):
    client, session = runtime_client(
        metrics={"capture_runtime_metrics": True, "flush_interval": 3}
    )
    state = native[1]
    state.time += 2
    state.user += 1
    state.system += 2
    timer = client.metrics._flush_timer
    timer.fire()
    metrics = exported_metrics(session)
    cpu = metrics["process.cpu.time"]
    memory = metrics["process.memory.usage"]
    assert cpu["unit"] == "s"
    assert cpu["sum"]["dataPoints"][0]["asDouble"] == 1
    assert memory["unit"] == "By"
    assert memory["sum"]["dataPoints"][0]["asDouble"] == state.rss
    tags = attributes(memory["sum"]["dataPoints"][0])
    assert tags["process.pid"] == os.getpid()
    assert attributes(sent_payload(session)["resourceMetrics"][0]["resource"])[
        "service.instance.id"
    ]
    assert "process.runtime_id" not in tags
    assert attributes(cpu["sum"]["dataPoints"][0]) == {
        **tags,
        "cpu.mode": "user",
    }
    assert client.metrics._flush_timer is not timer
    assert client.metrics._flush_timer.active
    assert client.metrics._flush_timer.interval == 3


@pytest.mark.parametrize("runtime_enabled", [False, True])
@pytest.mark.parametrize("outcome", ["ok", "retry-later"])
@pytest.mark.parametrize("reset_during_export", [False, True])
def test_timer_waits_for_export_before_starting_next_timer(
    runtime_client, monkeypatch, runtime_enabled, outcome, reset_during_export
):
    client, _ = runtime_client(metrics={"capture_runtime_metrics": runtime_enabled})
    metrics = client.metrics
    metrics.count("manual.before")
    metrics._consecutive_send_failures = 1
    entered, release = threading.Event(), threading.Event()

    def send(payload):
        entered.set()
        assert release.wait(5)
        return outcome

    sender = mock.Mock(side_effect=send)
    monkeypatch.setattr(metrics, "_send", sender)
    timer = metrics._flush_timer
    worker = threading.Thread(target=timer.fire)
    worker.start()
    try:
        assert entered.wait(5)
        if reset_during_export:
            metrics.reset()
        metrics.count("manual.during")
        pending = metrics._flush_timer
        assert pending is None or not pending.active
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert sender.call_count == 1
    assert metrics._flush_timer.active
    assert metrics._flush_timer.interval == metrics._flush_interval * (
        2 if outcome == "retry-later" else 1
    )


def test_runtime_samples_use_before_send(runtime_client, native):
    hook = mock.Mock(side_effect=lambda sample: None)
    client, session = runtime_client(
        metrics={"capture_runtime_metrics": True, "before_send": hook}
    )
    native[1].time += 10
    client.metrics._flush_timer.fire()
    assert hook.call_count == 18
    session.post.assert_not_called()
    assert client.metrics._flush_timer.active


def test_shutdown_collects_final_samples_and_stops_timer(runtime_client, native):
    client, session = runtime_client()
    timer = client.metrics._flush_timer
    native[1].time += 10
    native[1].user += 2.5
    client.shutdown()
    assert (
        exported_metrics(session)["process.cpu.time"]["sum"]["dataPoints"][0][
            "asDouble"
        ]
        == 2.5
    )
    assert not timer.active
    assert client.metrics._runtime_metrics is None
    assert client.metrics._flush_timer is None
    session.post.reset_mock()
    timer.fire()
    client.metrics.flush()
    session.post.assert_not_called()


def test_shutdown_from_before_send_stops_collection(runtime_client, native):
    client, _ = runtime_client()
    client.metrics._before_send = lambda sample: (client.shutdown(), sample)[1]
    native[1].time += 10
    client.metrics._flush_timer.fire()
    client.shutdown()
    assert client._shutdown_complete
    assert client.metrics._flush_timer is None
    assert not client.metrics._series


def test_shutdown_waits_for_inflight_collection(runtime_client, native):
    client, session = runtime_client()
    entered, release = threading.Event(), threading.Event()

    def hook(sample):
        entered.set()
        assert release.wait(5)
        return sample

    client.metrics._before_send = hook
    native[1].time += 10
    sampler = threading.Thread(target=client.metrics._flush_timer.fire)
    sampler.start()
    assert entered.wait(5)
    shutdown = threading.Thread(target=client.shutdown)
    shutdown.start()
    release.set()
    sampler.join(5)
    shutdown.join(5)
    assert not sampler.is_alive()
    assert not shutdown.is_alive()
    assert client._shutdown_complete
    assert client.metrics._runtime_metrics is None
    assert client.metrics._flush_timer is None
    assert not client.metrics._series
    assert session.post.called


def test_reset_clears_window_but_collection_continues(runtime_client, native):
    client, session = runtime_client()
    client.metrics.count("manual.metric")
    old_timer = client.metrics._flush_timer
    client.metrics.reset()
    assert not old_timer.active
    assert client.metrics._flush_timer.active
    native[1].time += 10
    client.metrics._flush_timer.fire()
    metrics = exported_metrics(session)
    assert "manual.metric" not in metrics
    assert "process.cpu.utilization" in metrics
    assert "process.memory.usage" in metrics


def test_runtime_collection_recovers_from_read_error(
    runtime_client, native, monkeypatch, caplog
):
    client, session = runtime_client()
    module, state, _ = native
    with monkeypatch.context() as patch:
        patch.setattr(
            module, "getrusage", mock.Mock(side_effect=OSError("unavailable"))
        )
        client.metrics._flush_timer.fire()
        client.metrics._flush_timer.fire()
    session.post.assert_not_called()
    assert caplog.text.count("Unable to collect runtime metrics") == 1
    assert client.metrics._flush_timer.active
    state.time += 10
    client.metrics._flush_timer.fire()
    assert "process.memory.usage" in exported_metrics(session)


def test_initialization_failure_does_not_break_client(
    runtime_client, native, monkeypatch, caplog
):
    monkeypatch.setattr(
        native[0].ctypes, "CDLL", mock.Mock(side_effect=OSError("missing libproc"))
    )
    client, _ = runtime_client()
    assert client.metrics._runtime_metrics is None
    assert client.metrics._flush_timer is None
    assert "Unable to start runtime metrics" in caplog.text
    client.metrics.count("manual.metric")
    client.metrics.flush()
    assert client.metrics._flush_timer is not None


def test_late_module_configuration_starts_collection(runtime_client, monkeypatch):
    for key, value in {
        "default_client": None,
        "api_key": "phc_test_key",
        "sync_mode": True,
        "metrics": None,
    }.items():
        monkeypatch.setattr(posthog, key, value)
    client = posthog.setup()
    try:
        assert client._metrics is None
        posthog.metrics = {"capture_runtime_metrics": True}
        assert posthog.setup() is client
        assert client.metrics._runtime_metrics is not None
        assert client.metrics._flush_timer.active
    finally:
        client.shutdown()


def test_shutdown_waits_for_runtime_metrics_initialization(
    runtime_client, native, monkeypatch
):
    client, _ = runtime_client(metrics=None)
    client._metrics_config = {"capture_runtime_metrics": True}
    entered, release = threading.Event(), threading.Event()
    stopping, stopped = threading.Event(), threading.Event()
    constructor = native[0]._MacOSProcessMetrics
    original_stop = metrics_capture.PostHogMetrics._stop_runtime_metrics

    def initialize():
        entered.set()
        assert release.wait(5)
        return constructor()

    def stop(metrics):
        stopping.set()
        original_stop(metrics)
        stopped.set()

    monkeypatch.setattr(native[0], "_MacOSProcessMetrics", initialize)
    monkeypatch.setattr(metrics_capture.PostHogMetrics, "_stop_runtime_metrics", stop)
    startup = threading.Thread(target=client._start_runtime_metrics)
    shutdown = threading.Thread(target=client.shutdown)
    startup.start()
    try:
        assert entered.wait(5)
        shutdown.start()
        assert stopping.wait(5)
        assert not stopped.wait(0.1)
    finally:
        release.set()
        startup.join(5)
        if shutdown.ident is not None:
            shutdown.join(5)
    assert not startup.is_alive() and not shutdown.is_alive()
    assert client._shutdown_complete
    assert client._metrics._runtime_metrics is None
    assert client._metrics._flush_timer is None


def test_shutdown_during_initialization_prevents_collector_install(
    runtime_client, native, monkeypatch
):
    client, session = runtime_client(metrics=None)
    client._metrics_config = {"capture_runtime_metrics": True}
    constructor = native[0]._MacOSProcessMetrics

    def initialize():
        client.shutdown()
        return constructor()

    monkeypatch.setattr(native[0], "_MacOSProcessMetrics", initialize)
    client._start_runtime_metrics()
    assert client._shutdown_complete
    assert client._metrics._runtime_metrics is None
    assert client._metrics._flush_timer is None
    session.post.assert_not_called()


def test_zero_elapsed_time_skips_cpu_utilization(native):
    collector = native[0]._MacOSProcessMetrics()
    samples = {sample.name: sample.value for sample in collector.collect()}
    assert "app.process.cpu.percent" not in samples
    assert "process.cpu.utilization" not in samples
    assert "process.cpu.time" in samples
    assert samples["process.memory.usage"] == native[1].rss
    assert samples["process.thread.count"] == 7
    assert samples["process.context_switches"] == 0


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_fork_replaces_locks_identity_and_cpu_baseline(runtime_client, native):
    client, _ = runtime_client()
    parent_id = client.metrics._runtime_metrics.resource_attributes[
        "service.instance.id"
    ]
    native[1].time += 100
    native[1].user += 50
    native[1].involuntary += 100000
    locks = [
        client.metrics._lock,
        client.metrics._flush_lock,
        client.metrics._runtime_lock,
    ]
    for lock in locks:
        lock.acquire()
    pid = os.fork()
    if pid == 0:
        signal.alarm(5)
        try:
            collector = client.metrics._runtime_metrics
            assert collector.attributes["process.pid"] == os.getpid()
            assert collector.resource_attributes["service.instance.id"] != parent_id
            assert collector.resource_attributes["host.name"] == "test-host"
            assert client.metrics._flush_timer.active
            collected = collector.collect()
            samples = {sample.name: sample.value for sample in collected}
            assert "app.process.cpu.percent" not in samples
            assert "process.cpu.utilization" not in samples
            assert all(
                sample.value == 0
                for sample in collected
                if sample.metric_type == "observable_counter"
            )
            assert samples["process.memory.usage"] == native[1].rss
            native[1].time += 10
            native[1].user += 1
            client.metrics.flush()
            client.shutdown()
        except BaseException:
            os._exit(1)
        os._exit(0)
    for lock in locks:
        lock.release()
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert (
        client.metrics._runtime_metrics.resource_attributes["service.instance.id"]
        == parent_id
    )


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_fork_during_timer_export_preserves_child_timer(runtime_client, monkeypatch):
    client, _ = runtime_client()
    metrics = client.metrics
    parent_pid = os.getpid()
    children = []

    def send(payload):
        pid = os.fork()
        if pid == 0:
            signal.alarm(5)
        children.append(pid)
        return "ok"

    monkeypatch.setattr(metrics, "_send", send)
    try:
        metrics._flush_timer.fire()
        if os.getpid() != parent_pid:
            assert not metrics._timer_flush_in_progress
            assert metrics._flush_timer.active
    except BaseException:
        if os.getpid() != parent_pid:
            os._exit(1)
        raise
    if os.getpid() != parent_pid:
        os._exit(0)
    _, status = os.waitpid(children[0], 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    monkeypatch.setattr(metrics, "_send", lambda payload: "ok")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_shutdown_from_hook_during_concurrent_shutdown_does_not_deadlock(
    runtime_client, native
):
    client, _ = runtime_client()
    pid = os.fork()
    if pid == 0:
        signal.alarm(5)
        try:
            entered = threading.Event()

            def hook(sample):
                entered.set()
                while client._lifecycle_owner is None:
                    time.sleep(0.001)
                client.shutdown()
                return sample

            client.metrics._before_send = hook
            native[1].time += 10
            sampler = threading.Thread(target=client.metrics._flush_timer.fire)
            sampler.start()
            assert entered.wait(2)
            shutdown = threading.Thread(target=client.shutdown)
            shutdown.start()
            sampler.join(2)
            shutdown.join(2)
            assert not sampler.is_alive() and not shutdown.is_alive()
            assert client._shutdown_complete
        except BaseException:
            os._exit(1)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS libproc")
def test_native_macos_current_process_reading():
    module = importlib.import_module("posthog._runtime_metrics")
    collector = module._MacOSProcessMetrics()
    samples = {
        sample.name: (sample.value, sample.unit) for sample in collector.collect()
    }
    rss, unit = samples["process.memory.usage"]
    assert rss > 0
    assert unit == "By"
    utilization, unit = samples["process.memory.utilization"]
    assert unit == "1"
    assert utilization == rss / (
        os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    )
    assert 0 < utilization <= 1
    assert samples["process.thread.count"][0] > 0
    assert samples["process.cpu.time"][0] >= 0
    assert samples["process.context_switches"][0] >= 0
    assert samples["cpython.gc.collections"][0] >= 0
    assert samples["cpython.gc.collected_objects"][0] >= 0
    assert samples["cpython.gc.uncollectable_objects"][0] >= 0
    assert ctypes.sizeof(module._ProcTaskInfo) == 96
    assert module._ProcTaskInfo.pti_resident_size.offset == 8
