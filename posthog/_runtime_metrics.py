import ctypes
import logging
import os
import sys
from dataclasses import dataclass
from gc import get_stats
from resource import RUSAGE_SELF, getrusage
from time import monotonic, time
from uuid import uuid4

log = logging.getLogger("posthog")


class _ProcTaskInfo(ctypes.Structure):
    # ABI layout of proc_taskinfo in macOS <sys/proc_info.h>.
    _fields_ = [
        ("pti_virtual_size", ctypes.c_uint64),
        ("pti_resident_size", ctypes.c_uint64),
        ("pti_total_user", ctypes.c_uint64),
        ("pti_total_system", ctypes.c_uint64),
        ("pti_threads_user", ctypes.c_uint64),
        ("pti_threads_system", ctypes.c_uint64),
        ("pti_policy", ctypes.c_int32),
        ("pti_faults", ctypes.c_int32),
        ("pti_pageins", ctypes.c_int32),
        ("pti_cow_faults", ctypes.c_int32),
        ("pti_messages_sent", ctypes.c_int32),
        ("pti_messages_received", ctypes.c_int32),
        ("pti_syscalls_mach", ctypes.c_int32),
        ("pti_syscalls_unix", ctypes.c_int32),
        ("pti_csw", ctypes.c_int32),
        ("pti_threadnum", ctypes.c_int32),
        ("pti_numrunning", ctypes.c_int32),
        ("pti_priority", ctypes.c_int32),
    ]


@dataclass(frozen=True)
class _RuntimeMetric:
    name: str
    value: float
    unit: str
    metric_type: str = "observable_counter"
    attributes: dict | None = None


class _MacOSProcessMetrics:
    def __init__(self) -> None:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self._proc_pidinfo = libproc.proc_pidinfo
        self._proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self._proc_pidinfo.restype = ctypes.c_int
        self._native_error_warned = False
        self._reset()

    def _reset(self) -> None:
        self._pid = os.getpid()
        self.attributes = {
            "process.pid": self._pid,
            "process.runtime_id": uuid4().hex,
        }
        self.start_time_ms = int(time() * 1000)
        usage = getrusage(RUSAGE_SELF)
        self._cpu_time_user = usage.ru_utime
        self._cpu_time_sys = usage.ru_stime
        self._time = monotonic()
        self._ctx_switches: int | None = None
        self._ctx_total = 0
        try:
            self._update_context_switches(self._read_task_info().pti_csw)
        except OSError:
            # CPU and GC sampling can still run when libproc is unavailable.
            pass

    def _read_task_info(self) -> _ProcTaskInfo:
        info = _ProcTaskInfo()
        size = ctypes.sizeof(info)
        read = self._proc_pidinfo(self._pid, 4, 0, ctypes.byref(info), size)
        if read != size:
            raise OSError(
                "proc_pidinfo(PROC_PIDTASKINFO) returned incomplete process information"
            )
        return info

    def _update_context_switches(self, current: int) -> int:
        if self._ctx_switches is None:
            self._ctx_total = current % (1 << 32)
        else:
            # libproc's signed 32-bit context-switch field wraps.
            self._ctx_total += (current - self._ctx_switches) % (1 << 32)
        self._ctx_switches = current
        return self._ctx_total

    def collect(self) -> list[_RuntimeMetric]:
        if self._pid != os.getpid():
            self._reset()
        usage = getrusage(RUSAGE_SELF)
        now = monotonic()
        elapsed = now - self._time
        user_delta = usage.ru_utime - self._cpu_time_user
        sys_delta = usage.ru_stime - self._cpu_time_sys
        self._cpu_time_user = usage.ru_utime
        self._cpu_time_sys = usage.ru_stime
        self._time = now

        samples = [
            _RuntimeMetric(
                "process.cpu.time", usage.ru_utime, "s", attributes={"cpu.mode": "user"}
            ),
            _RuntimeMetric(
                "process.cpu.time",
                usage.ru_stime,
                "s",
                attributes={"cpu.mode": "system"},
            ),
        ]
        if elapsed > 0 and user_delta >= 0 and sys_delta >= 0:
            samples.append(
                _RuntimeMetric(
                    "app.process.cpu.percent",
                    (user_delta + sys_delta) / elapsed * 100,
                    "%",
                    "gauge",
                )
            )
            cpus = getattr(os, "process_cpu_count", os.cpu_count)()
            if cpus is not None and cpus > 0:
                for mode, delta in (("user", user_delta), ("system", sys_delta)):
                    samples.append(
                        _RuntimeMetric(
                            "process.cpu.utilization",
                            delta / elapsed / cpus,
                            "1",
                            "gauge",
                            {"cpu.mode": mode},
                        )
                    )
        if sys.implementation.name == "cpython":
            for generation, stats in enumerate(get_stats()):
                for name, field, unit in (
                    ("collections", "collections", "{collection}"),
                    ("collected_objects", "collected", "{object}"),
                    ("uncollectable_objects", "uncollectable", "{object}"),
                ):
                    samples.append(
                        _RuntimeMetric(
                            f"cpython.gc.{name}",
                            stats[field],
                            unit,
                            attributes={"cpython.gc.generation": generation},
                        )
                    )
        try:
            info = self._read_task_info()
            samples.append(
                _RuntimeMetric(
                    "process.memory.usage",
                    info.pti_resident_size,
                    "By",
                    "observable_updowncounter",
                )
            )
            try:
                physical_pages = os.sysconf("SC_PHYS_PAGES")
                page_size = os.sysconf("SC_PAGE_SIZE")
            except (OSError, ValueError):
                pass
            else:
                if physical_pages > 0 and page_size > 0:
                    samples.append(
                        _RuntimeMetric(
                            "process.memory.utilization",
                            info.pti_resident_size / (physical_pages * page_size),
                            "1",
                            "gauge",
                        )
                    )
            samples.append(
                _RuntimeMetric(
                    "process.thread.count",
                    info.pti_threadnum,
                    "{thread}",
                    "observable_updowncounter",
                )
            )
            samples.append(
                _RuntimeMetric(
                    "app.process.cpu.ctx_switch.total",
                    self._update_context_switches(info.pti_csw),
                    "{context_switch}",
                )
            )
        except OSError as e:
            if not self._native_error_warned:
                self._native_error_warned = True
                log.warning("Unable to collect native process metrics: %s", e)
        return samples
