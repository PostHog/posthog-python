from unittest import mock

import pytest

import _example_runtime_metrics as workload


def test_default_duration_is_thirty_minutes():
    assert workload.run_workload.__defaults__ == (1800, 30)


@pytest.mark.parametrize(
    "cpus,ram_mib,expected_workers,expected_memory",
    [(1, 128, 1, 4), (8, 8192, 4, 256), (32, 65536, 8, 1024)],
)
def test_workload_repeats_phases_with_bounded_resources(
    monkeypatch, cpus, ram_mib, expected_workers, expected_memory
):
    clock = mock.Mock(elapsed=0.0)
    monkeypatch.setattr(workload.time, "monotonic", lambda: clock.elapsed)
    monkeypatch.setattr(
        workload.time,
        "sleep",
        lambda seconds: setattr(clock, "elapsed", clock.elapsed + seconds),
    )
    monkeypatch.setattr(workload.os, "cpu_count", lambda: cpus)
    monkeypatch.setattr(workload.os, "process_cpu_count", lambda: cpus, raising=False)
    monkeypatch.setattr(
        workload.os,
        "sysconf",
        lambda key: ram_mib * 256 if key == "SC_PHYS_PAGES" else 4096,
    )
    resize = mock.Mock()
    threads = mock.Mock()
    monkeypatch.setattr(workload, "_resize_memory", resize)
    monkeypatch.setattr(workload, "Thread", threads)
    monkeypatch.setattr(workload.gc, "collect", mock.Mock())
    workload.run_workload(duration=7, phase_duration=1)
    assert clock.elapsed == 7
    targets = [call.args[1] for call in resize.call_args_list]
    assert targets == [
        expected_memory // 8,
        expected_memory // 4,
        expected_memory // 4,
        expected_memory,
        expected_memory // 2,
        expected_memory // 8,
        expected_memory // 8,
    ]
    assert threads.call_count == 1 + 2 * expected_workers + max(
        1, expected_workers // 2
    )
    assert threads.return_value.start.call_count == threads.call_count
    assert threads.return_value.join.call_count == threads.call_count
    assert all(call.kwargs["args"][0].is_set() for call in threads.call_args_list)


def test_memory_resize_touches_pages_and_releases_excess_blocks():
    blocks = []
    workload._resize_memory(blocks, 16)
    assert sum(map(len, blocks)) == 16 * 1024 * 1024
    assert all(set(block[::4096]) == {1} for block in blocks)
    first = blocks[0]
    workload._resize_memory(blocks, 8)
    assert blocks == [first]
    workload._resize_memory(blocks, 0)
    assert blocks == []


def test_cpu_worker_hashes_large_buffers_until_stopped(monkeypatch):
    stop = mock.Mock()
    stop.is_set.side_effect = [False, False, True]
    hash_buffer = mock.Mock()
    monkeypatch.setattr(workload.hashlib, "sha256", hash_buffer)
    workload._burn_cpu(stop)
    assert hash_buffer.call_count == 2
    assert len(hash_buffer.call_args.args[0]) > 2047


def test_interrupt_stops_workers_and_releases_memory(monkeypatch):
    blocks = []

    def resize(allocations, target):
        allocations.append(bytearray(8))
        blocks.append(allocations)

    monkeypatch.setattr(workload, "_resize_memory", resize)
    monkeypatch.setattr(workload.gc, "collect", mock.Mock())
    threads = mock.Mock()
    monkeypatch.setattr(workload, "Thread", threads)
    clock = mock.Mock(elapsed=0.0)
    monkeypatch.setattr(workload.time, "monotonic", lambda: clock.elapsed)

    def sleep(seconds):
        if clock.elapsed:
            raise KeyboardInterrupt
        clock.elapsed += seconds

    monkeypatch.setattr(workload.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        workload.run_workload(duration=30, phase_duration=1)
    assert threads.call_count == 1
    assert all(not allocation for allocation in blocks)
    assert all(call.kwargs["args"][0].is_set() for call in threads.call_args_list)
    assert threads.return_value.join.call_count == threads.return_value.start.call_count
