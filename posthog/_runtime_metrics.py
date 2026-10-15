import ctypes
import logging
import os
import sys
from dataclasses import dataclass
from gc import get_stats
from resource import RUSAGE_SELF, getrusage
from socket import gethostname
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
        self._cpu_start_user = self._cpu_time_user = usage.ru_utime
        self._cpu_start_sys = self._cpu_time_sys = usage.ru_stime
        self._gc_start = get_stats() if sys.implementation.name == "cpython" else []
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
        if self._ctx_switches is not None:
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
                "process.cpu.time",
                usage.ru_utime - self._cpu_start_user,
                "s",
                attributes={"cpu.mode": "user"},
            ),
            _RuntimeMetric(
                "process.cpu.time",
                usage.ru_stime - self._cpu_start_sys,
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
                            stats[field] - self._gc_start[generation][field],
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


class _HostCPULoadInfo(ctypes.Structure):
    _fields_ = [("cpu_ticks", ctypes.c_uint32 * 4)]


class _VMStatistics64(ctypes.Structure):
    # Revision-1 prefix, not the growing latest vm_statistics64 ABI.
    _fields_ = (
        [
            (name, ctypes.c_uint32)
            for name in ("free_count", "active_count", "inactive_count", "wire_count")
        ]
        + [
            (name, ctypes.c_uint64)
            for name in (
                "zero_fill_count",
                "reactivations",
                "pageins",
                "pageouts",
                "faults",
                "cow_faults",
                "lookups",
                "hits",
                "purges",
            )
        ]
        + [(name, ctypes.c_uint32) for name in ("purgeable_count", "speculative_count")]
        + [
            (name, ctypes.c_uint64)
            for name in ("decompressions", "compressions", "swapins", "swapouts")
        ]
        + [
            (name, ctypes.c_uint32)
            for name in (
                "compressor_page_count",
                "throttled_count",
                "external_page_count",
                "internal_page_count",
            )
        ]
        + [("total_uncompressed_pages_in_compressor", ctypes.c_uint64)]
    )


class _SwapUsage(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_uint64),
        ("available", ctypes.c_uint64),
        ("used", ctypes.c_uint64),
        ("page_size", ctypes.c_uint32),
        ("encrypted", ctypes.c_int32),
    ]


class _MacOSHostMetrics:
    def __init__(self) -> None:
        self._lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        for name, arguments in (
            ("mach_host_self", []),
            ("mach_task_self", []),
            ("mach_port_deallocate", [ctypes.c_uint32, ctypes.c_uint32]),
            (
                "host_statistics",
                [
                    ctypes.c_uint32,
                    ctypes.c_int32,
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_uint32),
                ],
            ),
            (
                "host_statistics64",
                [
                    ctypes.c_uint32,
                    ctypes.c_int32,
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_uint32),
                ],
            ),
            ("host_page_size", [ctypes.c_uint32, ctypes.POINTER(ctypes.c_size_t)]),
            (
                "sysctlbyname",
                [
                    ctypes.c_char_p,
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_size_t),
                    ctypes.c_void_p,
                    ctypes.c_size_t,
                ],
            ),
        ):
            function = getattr(self._lib, name)
            function.argtypes = arguments
            function.restype = (
                ctypes.c_uint32
                if name in ("mach_host_self", "mach_task_self")
                else ctypes.c_int32
            )
        self._task_port = self._lib.mach_task_self()
        self.attributes = {"host.name": gethostname()}
        self.start_time_ms = int(time() * 1000)
        self._cpu_ticks: tuple[int, ...] | None = None
        self._swap_start: tuple[int, int] | None = None
        self._warned: set[str] = set()
        self.collect()

    def _read_statistics(self, flavor: int, info: ctypes.Structure) -> int:
        host = self._lib.mach_host_self()
        count = ctypes.c_uint32(ctypes.sizeof(info) // 4)
        page_size = ctypes.c_size_t()
        try:
            read = (
                self._lib.host_statistics64
                if flavor == 4
                else self._lib.host_statistics
            )
            result = read(host, flavor, ctypes.byref(info), ctypes.byref(count))
            if result != 0 or count.value != ctypes.sizeof(info) // 4:
                raise OSError(
                    f"host statistics flavor {flavor}: result={result}, count={count.value}"
                )
            if flavor == 4:
                result = self._lib.host_page_size(host, ctypes.byref(page_size))
                if result != 0 or page_size.value == 0:
                    raise OSError(f"host_page_size failed: result={result}")
        finally:
            self._lib.mach_port_deallocate(self._task_port, host)
        return page_size.value

    def _sysctl(self, name: bytes, value) -> None:
        size = ctypes.c_size_t(ctypes.sizeof(value))
        result = self._lib.sysctlbyname(
            name, ctypes.byref(value), ctypes.byref(size), None, 0
        )
        if result != 0:
            error = ctypes.get_errno()
            raise OSError(error, f"{name.decode()}: {os.strerror(error)}")
        if size.value != ctypes.sizeof(value):
            raise OSError(f"{name.decode()} returned incomplete information")

    def _cpu(self) -> list[_RuntimeMetric]:
        info = _HostCPULoadInfo()
        self._read_statistics(3, info)
        current = tuple(info.cpu_ticks)
        previous, self._cpu_ticks = self._cpu_ticks, current
        if previous is None:
            return []
        deltas = [(now - before) % (1 << 32) for now, before in zip(current, previous)]
        total = sum(deltas)
        if total == 0:
            return []
        return [
            _RuntimeMetric(
                "app.system.cpu.utilization", (total - deltas[2]) / total, "1", "gauge"
            )
        ]

    def _memory(self) -> list[_RuntimeMetric]:
        info = _VMStatistics64()
        page_size = self._read_statistics(4, info)
        if self._swap_start is None:
            self._swap_start = (info.swapins, info.swapouts)
        return [
            _RuntimeMetric(
                "app.system.memory.available.estimated",
                (info.free_count + info.inactive_count) * page_size,
                "By",
                "gauge",
            ),
            *[
                _RuntimeMetric(
                    "app.system.swap.io",
                    (current - baseline) * page_size,
                    "By",
                    attributes={"system.paging.direction": direction},
                )
                for direction, current, baseline in zip(
                    ("in", "out"), (info.swapins, info.swapouts), self._swap_start
                )
            ],
        ]

    def _swap_usage(self) -> list[_RuntimeMetric]:
        info = _SwapUsage()
        self._sysctl(b"vm.swapusage", info)
        return [
            _RuntimeMetric(
                "system.paging.usage",
                info.used,
                "By",
                "observable_updowncounter",
                {"system.paging.state": "used"},
            )
        ]

    def _pressure(self) -> list[_RuntimeMetric]:
        level = ctypes.c_uint32()
        self._sysctl(b"kern.memorystatus_vm_pressure_level", level)
        if level.value not in (1, 2, 4):
            raise OSError(f"Unknown memory pressure level: {level.value}")
        return [
            _RuntimeMetric(
                "app.system.memory.pressure", level.value, "{level}", "gauge"
            )
        ]

    def collect(self) -> list[_RuntimeMetric]:
        samples = []
        for name, read in (
            ("CPU", self._cpu),
            ("VM", self._memory),
            ("swap usage", self._swap_usage),
            ("memory pressure", self._pressure),
        ):
            try:
                samples.extend(read())
            except OSError as error:
                if name not in self._warned:
                    self._warned.add(name)
                    log.warning("Unable to collect host %s metrics: %s", name, error)
        return samples
