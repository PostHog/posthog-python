"""Process measurements; allocation instrumentation never runs in a timing/RSS pass."""

import gc
import resource
import sys
import tempfile
import time
from pathlib import Path

import psutil

PROCESS = psutil.Process()


def settle():
    gc.collect()
    time.sleep(0)
    gc.collect()


def memory():
    return {"rss": PROCESS.memory_info().rss}


def lifetime_peak_rss():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


def time_operations(run, operations):
    cpu_start = time.process_time_ns()
    thread_start = time.thread_time_ns()
    start = time.perf_counter_ns()
    run()
    elapsed = time.perf_counter_ns() - start
    thread_cpu = time.thread_time_ns() - thread_start
    cpu = time.process_time_ns() - cpu_start
    return {
        "elapsed_ns": elapsed,
        "elapsed_time_per_op": elapsed / operations,
        "process_cpu_time_per_op": cpu / operations,
        "caller_thread_cpu_time_per_op": thread_cpu / operations,
    }


def allocation_totals(path):
    import memray

    allocators = {
        memray.AllocatorType.PYMALLOC_MALLOC,
        memray.AllocatorType.PYMALLOC_CALLOC,
        memray.AllocatorType.PYMALLOC_REALLOC,
    }
    total_bytes = total_calls = 0
    with memray.FileReader(path) as reader:
        for record in reader.get_allocation_records():
            if record.allocator in allocators:
                total_bytes += record.size
                total_calls += record.n_allocations
    return {"allocated_bytes": total_bytes, "allocation_calls": total_calls}


def sample_allocations(run):
    import memray

    with tempfile.TemporaryDirectory(prefix="posthog-allocations-") as directory:
        path = Path(directory) / "allocations.bin"
        with memray.Tracker(
            path,
            trace_python_allocators=True,
            file_format=memray.FileFormat.ALL_ALLOCATIONS,
        ):
            run()
        return allocation_totals(path)


def classify_exit(*, timed_out=False, rss_limit_exceeded=False, stderr=""):
    if timed_out:
        return "timeout"
    # A parent-enforced RSS ceiling is a measured limit violation, not proof of OS OOM.
    if rss_limit_exceeded:
        return "error"
    if any(line.strip() == "MemoryError" for line in stderr.splitlines()):
        return "oom"
    return "error"
