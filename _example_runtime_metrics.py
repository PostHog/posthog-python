"""Bounded CPU and memory workload for example.py."""

import gc
import hashlib
import os
import time
from threading import Event, Thread


_MIB = 1024 * 1024
_BLOCK_SIZE = 8 * _MIB


def _burn_cpu(stop: Event) -> None:
    payload = b"x" * _MIB
    # Hashing more than 2047 bytes releases the GIL, allowing parallel CPU work.
    while not stop.is_set():
        hashlib.sha256(payload).digest()


def _resize_memory(blocks: list[bytearray], target_mib: int) -> None:
    target_blocks = target_mib * _MIB // _BLOCK_SIZE
    del blocks[target_blocks:]
    while len(blocks) < target_blocks:
        block = bytearray(_BLOCK_SIZE)
        block[::4096] = b"\x01" * (len(block) // 4096)
        blocks.append(block)


def run_workload(duration: float = 30 * 60, phase_duration: float = 30) -> None:
    cpu_count = getattr(os, "process_cpu_count", os.cpu_count)() or 1
    max_workers = max(1, min(8, cpu_count // 2))
    try:
        physical_memory = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        memory_mib = max(0, min(1024, physical_memory // 32 // _MIB))
    except (AttributeError, OSError, ValueError):
        memory_mib = 256
    phases = [
        ("idle", 0, memory_mib // 8),
        ("ramp-up", 1, memory_mib // 4),
        ("CPU burst", max_workers, memory_mib // 4),
        ("CPU and memory peak", max_workers, memory_mib),
        ("recovery", max(1, max_workers // 2), memory_mib // 2),
        ("idle", 0, memory_mib // 8),
    ]
    blocks: list[bytearray] = []
    start = time.monotonic()
    deadline = start + duration
    phase_index = 0
    try:
        while time.monotonic() < deadline:
            name, worker_count, target_mib = phases[phase_index % len(phases)]
            phase_end = min(deadline, start + (phase_index + 1) * phase_duration)
            _resize_memory(blocks, target_mib)
            for _ in range(10000):
                cycle: list[object] = []
                cycle.append(cycle)
            del cycle
            gc.collect()
            print(
                f"Phase: {name}; CPU workers: {worker_count}; "
                f"target memory: {target_mib} MiB; "
                f"elapsed: {time.monotonic() - start:.0f}s / {duration:.0f}s",
                flush=True,
            )
            stop = Event()
            workers = []
            try:
                for _ in range(worker_count):
                    worker = Thread(target=_burn_cpu, args=(stop,), daemon=True)
                    worker.start()
                    workers.append(worker)
                while time.monotonic() < phase_end:
                    time.sleep(min(1, max(0, phase_end - time.monotonic())))
            finally:
                stop.set()
                for worker in workers:
                    worker.join()
            phase_index += 1
    finally:
        blocks.clear()
        gc.collect()
