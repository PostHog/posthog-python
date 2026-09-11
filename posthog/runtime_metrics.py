"""Opt-in runtime metrics autocapture — samples host/process health on a timer
and feeds it into the existing ``client.metrics`` pipe (``/i/v1/metrics``).

Off by default. When enabled (``metrics_autocapture=True``) a single daemon
thread wakes every ``interval`` seconds, reads a small, low-cardinality set of
process metrics from the standard library — and, when ``psutil`` is installed,
a few richer ones — and records them through ``client.metrics``. Nothing new is
on the wire path: the metrics client already pre-aggregates and exports OTLP/JSON
with correct temporality, so this module only decides *what* to sample and *how
often*, never *how to send*.

Counters that the platform reports cumulatively (CPU seconds, GC collections)
are emitted as per-interval *deltas* through ``metrics.count`` — the metrics
client uses delta temporality, so each data point stands alone and a process
restart needs no cross-run state. Everything else is a ``gauge``.

Fork safety mirrors the metrics client: the sampler thread does not survive
``fork()``, so preforking servers (gunicorn, celery) restart it per worker via
the client's ``os.register_at_fork`` hook, and a fresh ``service.instance.id``
(``hostname-pid``) is stamped per process so workers don't collapse into one
series and clobber each other's gauges.
"""

import gc
import logging
import os
import socket
import sys
import threading
from typing import TYPE_CHECKING, Any, Optional

# Typed Any so the `resource is None` guard stays reachable under mypy on
# platforms (macOS/Linux) where the module always imports.
resource: Any
try:
    import resource
except ImportError:  # Windows has no `resource` module.
    resource = None

if TYPE_CHECKING:
    from posthog.metrics_capture import PostHogMetrics

log = logging.getLogger("posthog")

_DEFAULT_SAMPLE_INTERVAL_SECONDS = 60.0
# A floor so a misconfigured tiny interval can't turn a health probe into a
# busy loop that itself dominates the process's CPU/thread numbers.
_MIN_SAMPLE_INTERVAL_SECONDS = 1.0
# Bounds how long shutdown waits for the sampler thread to notice the stop
# signal; it only ever sleeps on an Event, so this is reached in practice.
_THREAD_JOIN_TIMEOUT_SECONDS = 5.0


def _import_psutil() -> Any:
    try:
        import psutil

        return psutil
    except Exception:
        return None


def _default_instance_id() -> str:
    # `hostname-pid`: unique per process so preforked workers keep separate
    # series. gethostname() can raise on exotic hosts — fall back to the pid.
    try:
        host = socket.gethostname() or "unknown-host"
    except Exception:
        host = "unknown-host"
    return f"{host}-{os.getpid()}"


class MetricsAutocapture:
    """Periodic runtime-metrics sampler feeding ``client.metrics``.

    Created by the client only when ``metrics_autocapture=True``. Public surface
    is just ``start``/``stop`` plus the fork hook the client calls.
    """

    def __init__(
        self,
        metrics: "PostHogMetrics",
        interval: float = _DEFAULT_SAMPLE_INTERVAL_SECONDS,
    ):
        self._metrics = metrics
        if (
            not isinstance(interval, (int, float))
            or isinstance(interval, bool)
            or not interval > 0
        ):
            log.warning(
                "Ignoring metrics autocapture interval %r: expected a positive number of seconds",
                interval,
            )
            interval = _DEFAULT_SAMPLE_INTERVAL_SECONDS
        self._interval = max(float(interval), _MIN_SAMPLE_INTERVAL_SECONDS)

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # True between start() and stop(): the intent to be running. Consulted on
        # fork so a child of an already-stopped client doesn't spawn a thread.
        self._active = False
        self._pid = os.getpid()
        # Last cumulative reading per counter series, so we can emit deltas.
        self._prev: dict = {}
        self._psutil = _import_psutil()
        self._psutil_process: Any = None
        # Manage a default instance id only if the user didn't set one, so their
        # override survives; remember it so the fork hook can refresh the pid.
        self._manages_instance_id = (
            "service.instance.id" not in self._metrics._resource_attributes
        )
        self._apply_instance_id()

    def _apply_instance_id(self) -> None:
        if not self._manages_instance_id:
            return
        try:
            self._metrics._resource_attributes["service.instance.id"] = (
                _default_instance_id()
            )
        except Exception:
            pass

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._active = True
            self._stop_event = threading.Event()
            # Prime cumulative baselines so the first interval emits a real delta
            # rather than the whole since-process-start total.
            self._prev = {}
            self._prime_baselines()
            thread = threading.Thread(
                target=self._run,
                name="posthog-metrics-autocapture",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def stop(self, final_sample: bool = True) -> None:
        """Stop the sampler. By default takes one last sample so the final
        window (flushed by the client on shutdown) isn't a blind spot."""
        with self._lock:
            self._active = False
            self._stop_event.set()
            thread = self._thread
            self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)
        if final_sample:
            self._sample_guarded()

    def _run(self) -> None:
        # wait() returns True only when the stop Event is set, so this exits
        # promptly on stop() and otherwise ticks once per interval.
        while not self._stop_event.wait(self._interval):
            self._sample_guarded()

    def _reinit_after_fork(self) -> None:
        # Runs in a forked child (via the client's fork hook) before user code.
        # The inherited thread does not exist here and the lock may be held by a
        # vanished parent thread — replace state without acquiring anything.
        was_active = self._active
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._pid = os.getpid()
        self._prev = {}
        # psutil.Process caches the pid it was built for; drop it so the child
        # rebuilds against its own process.
        self._psutil_process = None
        self._apply_instance_id()
        if was_active:
            self.start()

    def _sample_guarded(self) -> None:
        # A telemetry timer must never raise into the host application, and one
        # failing collector must not skip the rest — each collector guards itself.
        for collector in (
            self._collect_rusage,
            self._collect_threads,
            self._collect_gc,
            self._collect_loadavg,
            self._collect_psutil,
        ):
            try:
                collector()
            except Exception as e:
                log.debug("metrics autocapture collector failed: %s", e)

    def _prime_baselines(self) -> None:
        # Record current cumulative values without emitting, so the first real
        # sample's deltas are measured from start(), not from process boot.
        for primer in (self._prime_rusage, self._prime_gc):
            try:
                primer()
            except Exception:
                pass

    def _emit_counter_delta(
        self, key: str, name: str, current: float, unit=None, attributes=None
    ) -> None:
        prev = self._prev.get(key)
        self._prev[key] = current
        if prev is None:
            return
        delta = current - prev
        # A negative delta means the cumulative source reset (should not happen
        # for these) — skip it rather than feed the monotonic counter a drop.
        if delta <= 0:
            return
        self._metrics.count(name, delta, unit=unit, attributes=attributes)

    def _maxrss_bytes(self, ru_maxrss: int) -> int:
        # getrusage reports ru_maxrss in bytes on macOS but kilobytes on Linux
        # and the BSDs; normalize to bytes. (Ternary, not an if/return, so mypy's
        # host-platform narrowing doesn't flag one branch as unreachable.)
        multiplier = 1 if sys.platform == "darwin" else 1024
        return int(ru_maxrss) * multiplier

    def _prime_rusage(self) -> None:
        if resource is None:
            return
        usage = resource.getrusage(resource.RUSAGE_SELF)
        self._prev["cpu.user"] = usage.ru_utime
        self._prev["cpu.system"] = usage.ru_stime

    def _collect_rusage(self) -> None:
        if resource is None:
            return
        usage = resource.getrusage(resource.RUSAGE_SELF)
        self._emit_counter_delta(
            "cpu.user",
            "process.cpu.time",
            usage.ru_utime,
            unit="s",
            attributes={"state": "user"},
        )
        self._emit_counter_delta(
            "cpu.system",
            "process.cpu.time",
            usage.ru_stime,
            unit="s",
            attributes={"state": "system"},
        )
        self._metrics.gauge(
            "process.memory.peak_rss",
            self._maxrss_bytes(usage.ru_maxrss),
            unit="By",
        )

    def _collect_threads(self) -> None:
        self._metrics.gauge("process.thread.count", threading.active_count())

    def _prime_gc(self) -> None:
        for generation, stats in enumerate(gc.get_stats()):
            self._prev[f"gc.collections.{generation}"] = stats.get("collections", 0)

    def _collect_gc(self) -> None:
        for generation, stats in enumerate(gc.get_stats()):
            self._emit_counter_delta(
                f"gc.collections.{generation}",
                "process.runtime.gc_collections",
                stats.get("collections", 0),
                attributes={"generation": generation},
            )

    def _collect_loadavg(self) -> None:
        getloadavg = getattr(os, "getloadavg", None)
        if getloadavg is None:  # Not available on Windows.
            return
        one, five, fifteen = getloadavg()
        self._metrics.gauge("system.cpu.load_average.1m", one)
        self._metrics.gauge("system.cpu.load_average.5m", five)
        self._metrics.gauge("system.cpu.load_average.15m", fifteen)

    def _collect_psutil(self) -> None:
        if self._psutil is None:
            return
        if self._psutil_process is None:
            self._psutil_process = self._psutil.Process()
        proc = self._psutil_process

        try:
            # interval=None returns utilization since the previous call; the
            # priming call at Process() creation makes the first real value meaningful.
            self._metrics.gauge(
                "process.cpu.utilization", proc.cpu_percent(None), unit="%"
            )
        except Exception:
            pass
        try:
            mem = proc.memory_info()
            self._metrics.gauge("process.memory.usage", mem.rss, unit="By")
            self._metrics.gauge("process.memory.virtual", mem.vms, unit="By")
        except Exception:
            pass
        num_fds = getattr(proc, "num_fds", None)
        if num_fds is not None:  # POSIX only.
            try:
                self._metrics.gauge("process.open_file_descriptors", num_fds())
            except Exception:
                pass
