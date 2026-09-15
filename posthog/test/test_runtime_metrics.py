import gzip
import json
from unittest import mock

import pytest

from posthog.client import Client
from posthog.runtime_metrics import (
    _DEFAULT_SAMPLE_INTERVAL_SECONDS,
    _MIN_SAMPLE_INTERVAL_SECONDS,
    MetricsAutocapture,
    _default_instance_id,
)

FAKE_API_KEY = "phc_test_key"


class RecordingMetrics:
    """Stand-in for PostHogMetrics that records what the sampler pushes."""

    def __init__(self, resource_attributes=None):
        self.counts = []
        self.gauges = []
        self._resource_attributes = resource_attributes or {}

    def count(self, name, value=1, unit=None, attributes=None):
        self.counts.append((name, value, unit, attributes))

    def gauge(self, name, value, unit=None, attributes=None):
        self.gauges.append((name, value, unit, attributes))


def mock_session(status_code=200):
    session = mock.Mock()
    session.post.return_value = mock.Mock(status_code=status_code)
    return session


class TestCounterDeltas:
    def test_first_reading_is_baseline_only(self):
        m = RecordingMetrics()
        ac = MetricsAutocapture(m)
        ac._emit_counter_delta("k", "process.cpu.time", 5.0)
        assert m.counts == []

    def test_second_reading_emits_delta_not_cumulative(self):
        m = RecordingMetrics()
        ac = MetricsAutocapture(m)
        ac._emit_counter_delta("k", "process.cpu.time", 5.0)
        ac._emit_counter_delta("k", "process.cpu.time", 7.5, unit="s")
        assert m.counts == [("process.cpu.time", 2.5, "s", None)]

    def test_zero_delta_is_not_emitted(self):
        m = RecordingMetrics()
        ac = MetricsAutocapture(m)
        ac._prev["k"] = 5.0
        ac._emit_counter_delta("k", "process.cpu.time", 5.0)
        assert m.counts == []

    def test_counter_reset_is_skipped(self):
        m = RecordingMetrics()
        ac = MetricsAutocapture(m)
        ac._prev["k"] = 10.0
        ac._emit_counter_delta("k", "process.cpu.time", 3.0)
        assert m.counts == []
        # The reset value still becomes the new baseline.
        assert ac._prev["k"] == 3.0


class TestInstanceId:
    def test_default_instance_id_is_hostname_pid(self):
        m = RecordingMetrics()
        MetricsAutocapture(m)
        instance_id = m._resource_attributes["service.instance.id"]
        assert instance_id
        assert instance_id.endswith(f"-{__import__('os').getpid()}")

    def test_user_supplied_instance_id_is_preserved(self):
        m = RecordingMetrics({"service.instance.id": "mine"})
        MetricsAutocapture(m)
        assert m._resource_attributes["service.instance.id"] == "mine"

    def test_default_instance_id_helper_survives_hostname_failure(self):
        with mock.patch(
            "posthog.runtime_metrics.socket.gethostname",
            side_effect=OSError("boom"),
        ):
            assert _default_instance_id().startswith("unknown-host-")


class TestInterval:
    def test_invalid_interval_falls_back_to_default(self):
        m = RecordingMetrics()
        assert (
            MetricsAutocapture(m, interval=0)._interval
            == _DEFAULT_SAMPLE_INTERVAL_SECONDS
        )
        assert (
            MetricsAutocapture(m, interval=-5)._interval
            == _DEFAULT_SAMPLE_INTERVAL_SECONDS
        )
        assert (
            MetricsAutocapture(m, interval=True)._interval
            == _DEFAULT_SAMPLE_INTERVAL_SECONDS
        )

    def test_tiny_interval_is_clamped_to_floor(self):
        m = RecordingMetrics()
        assert (
            MetricsAutocapture(m, interval=0.001)._interval
            == _MIN_SAMPLE_INTERVAL_SECONDS
        )


class TestCollectors:
    def test_sample_emits_core_stdlib_metrics(self):
        m = RecordingMetrics()
        ac = MetricsAutocapture(m)
        ac._prime_baselines()
        ac._sample_guarded()
        gauge_names = {g[0] for g in m.gauges}
        assert "process.thread.count" in gauge_names
        assert "process.memory.peak_rss" in gauge_names

    def test_maxrss_normalized_to_bytes_per_platform(self):
        m = RecordingMetrics()
        ac = MetricsAutocapture(m)
        with mock.patch("posthog.runtime_metrics.sys.platform", "darwin"):
            assert ac._maxrss_bytes(1000) == 1000
        with mock.patch("posthog.runtime_metrics.sys.platform", "linux"):
            assert ac._maxrss_bytes(1000) == 1024000

    def test_one_failing_collector_does_not_skip_the_rest(self):
        m = RecordingMetrics()
        ac = MetricsAutocapture(m)
        with mock.patch.object(ac, "_collect_rusage", side_effect=RuntimeError("boom")):
            ac._sample_guarded()
        assert any(g[0] == "process.thread.count" for g in m.gauges)

    def test_psutil_metrics_collected_when_available(self):
        fake_proc = mock.Mock()
        fake_proc.cpu_percent.return_value = 12.0
        fake_proc.memory_info.return_value = mock.Mock(rss=1000, vms=2000)
        fake_proc.num_fds.return_value = 7
        fake_psutil = mock.Mock()
        fake_psutil.Process.return_value = fake_proc

        m = RecordingMetrics()
        ac = MetricsAutocapture(m)
        ac._psutil = fake_psutil
        ac._collect_psutil()

        by_name = {g[0]: g[1] for g in m.gauges}
        assert by_name["process.cpu.utilization"] == 12.0
        assert by_name["process.memory.usage"] == 1000
        assert by_name["process.memory.virtual"] == 2000
        assert by_name["process.open_file_descriptors"] == 7

    def test_psutil_absent_is_a_no_op(self):
        m = RecordingMetrics()
        ac = MetricsAutocapture(m)
        ac._psutil = None
        ac._collect_psutil()
        assert m.gauges == []


class TestLifecycle:
    def test_start_spawns_thread_stop_removes_it(self):
        m = RecordingMetrics()
        ac = MetricsAutocapture(m, interval=60)
        ac.start()
        try:
            assert ac._thread is not None and ac._thread.is_alive()
        finally:
            ac.stop(final_sample=False)
        assert ac._thread is None

    def test_stop_takes_a_final_sample(self):
        m = RecordingMetrics()
        ac = MetricsAutocapture(m, interval=60)
        ac.start()
        m.gauges.clear()
        m.counts.clear()
        ac.stop()
        assert len(m.gauges) > 0

    def test_reinit_after_fork_restarts_when_active(self):
        m = RecordingMetrics()
        ac = MetricsAutocapture(m, interval=60)
        ac.start()
        try:
            ac._prev["stale"] = 1
            ac._reinit_after_fork()
            assert "stale" not in ac._prev
            assert ac._thread is not None and ac._thread.is_alive()
        finally:
            ac.stop(final_sample=False)

    def test_reinit_after_fork_does_not_restart_when_stopped(self):
        m = RecordingMetrics()
        ac = MetricsAutocapture(m, interval=60)
        ac._reinit_after_fork()
        assert ac._thread is None


class TestClientWiring:
    def test_off_by_default(self):
        c = Client(FAKE_API_KEY, host="https://us.example.com", sync_mode=True)
        assert c._metrics_autocapture is None

    def test_disabled_client_does_not_start_autocapture(self):
        c = Client(
            FAKE_API_KEY,
            host="https://us.example.com",
            sync_mode=True,
            disabled=True,
            metrics_autocapture=True,
        )
        assert c._metrics_autocapture is None

    def test_enabled_creates_a_running_sampler(self):
        c = Client(
            FAKE_API_KEY,
            host="https://us.example.com",
            sync_mode=True,
            metrics_autocapture=True,
        )
        try:
            assert c._metrics_autocapture is not None
            assert c._metrics_autocapture._thread.is_alive()
        finally:
            c._metrics_autocapture.stop(final_sample=False)
            c.metrics.reset()

    def test_interval_read_from_metrics_config(self):
        c = Client(
            FAKE_API_KEY,
            host="https://us.example.com",
            sync_mode=True,
            metrics_autocapture=True,
            metrics={"autocapture_interval": 30},
        )
        try:
            assert c._metrics_autocapture._interval == 30
        finally:
            c._metrics_autocapture.stop(final_sample=False)
            c.metrics.reset()

    def test_samples_flow_through_to_the_metrics_wire(self):
        c = Client(
            FAKE_API_KEY,
            host="https://us.example.com",
            sync_mode=True,
            metrics_autocapture=True,
        )
        try:
            c._metrics_autocapture._sample_guarded()
            session = mock_session()
            with mock.patch(
                "posthog.metrics_capture._get_session", return_value=session
            ):
                c.metrics.flush()
            assert session.post.called
            _, kwargs = session.post.call_args
            payload = json.loads(gzip.decompress(kwargs["data"]).decode("utf-8"))
            resource = payload["resourceMetrics"][0]["resource"]["attributes"]
            keys = {kv["key"] for kv in resource}
            assert "service.instance.id" in keys
            metric_names = {
                mtr["name"]
                for mtr in payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
            }
            assert "process.thread.count" in metric_names
        finally:
            c._metrics_autocapture.stop(final_sample=False)
            c.metrics.reset()


@pytest.mark.skipif(
    not hasattr(__import__("os"), "getloadavg"),
    reason="os.getloadavg is not available on this platform",
)
def test_loadavg_emits_three_windows():
    m = RecordingMetrics()
    ac = MetricsAutocapture(m)
    ac._collect_loadavg()
    names = {g[0] for g in m.gauges}
    assert {
        "system.cpu.load_average.1m",
        "system.cpu.load_average.5m",
        "system.cpu.load_average.15m",
    } <= names
