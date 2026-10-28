import copy
import gc
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError

import memray
from fixtures import USERS, definitions, digest, expected_flags, workloads
from identity import environment, source_digest
from measurement import (
    allocation_totals,
    classify_exit,
    memory,
    sample_allocations,
    settle,
    time_operations,
)
from prepare import prepare
from run import append_sample, load_shards, main, run_worker
from server import BenchmarkServer
from worker import execute
from workload import Workload, control

from posthog import Posthog

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv-benchmark/bin/python"


class BenchmarksTest(unittest.TestCase):
    def test_specification_inventory_and_parameters(self):
        single_cases = (
            "boolean",
            "rollout",
            "variant",
            "property",
            "regex",
            "cohort",
            "early",
            "late",
            "no-match",
        )
        for smoke in (False, True):
            expected = {
                **{
                    f"flags.single.{case}": (
                        160 if smoke else 16000,
                        ["timing", "allocation"],
                    )
                    for case in single_cases
                },
                **{
                    f"flags.all.{count}": (
                        16 if smoke else operations,
                        ["timing", "allocation"],
                    )
                    for count, operations in ((10, 1600), (100, 160), (1000, 160))
                },
                "flags.load.1000": (32 if smoke else 1600, ["memory"]),
                "flags.refresh.1000": (4 if smoke else 40, ["memory"]),
                **{
                    f"capture.enqueue.{size}": (200 if smoke else 1000, ["timing"])
                    for size in (32, 4096)
                },
                **{
                    f"capture.drain.{strategy}.{size}": (
                        200 if smoke else 1000,
                        ["timing", "allocation", "memory"],
                    )
                    for strategy in ("steady", "burst")
                    for size in (32, 4096)
                },
            }
            specs = workloads(smoke)
            self.assertEqual(len(specs), 20)
            self.assertEqual(
                {s["id"]: (s["operations"], s["passes"]) for s in specs}, expected
            )
            for spec in specs:
                self.assertEqual(
                    spec["concurrency"], 8 if spec["id"] == "flags.load.1000" else 1
                )
                if spec["mode"] == "single":
                    self.assertEqual(spec["flag_count"], 10)
                    self.assertEqual(
                        spec["flag_index"],
                        single_cases.index(spec["id"].split(".")[-1]),
                    )
                    self.assertEqual(
                        spec["allocation_operations"], 160 if smoke else 2000
                    )
                elif "flag_count" in spec:
                    self.assertEqual(spec["flag_count"], int(spec["id"].split(".")[-1]))
                    if "allocation_operations" in spec:
                        self.assertEqual(
                            spec["allocation_operations"],
                            16 if smoke else max(16, 1600 // spec["flag_count"]),
                        )
                else:
                    self.assertEqual(
                        spec["payload_bytes"], int(spec["id"].split(".")[-1])
                    )
                    self.assertEqual(
                        spec["completion_boundary"],
                        "api_return"
                        if spec["mode"] == "capture"
                        else "completed_delivery",
                    )
                    if spec["mode"] == "drain":
                        self.assertEqual(
                            spec["drain_strategy"], spec["id"].split(".")[2]
                        )
                        self.assertEqual(
                            spec["flush_every"],
                            100
                            if spec["drain_strategy"] == "steady"
                            else spec["operations"],
                        )

    def test_shard_manifest_exactly_partitions_full_and_smoke(self):
        shards = load_shards()
        self.assertEqual(
            list(shards), ["flags-single", "flags-scaling", "flags-memory", "capture"]
        )
        assigned = [item for items in shards.values() for item in items]
        for smoke in (False, True):
            self.assertEqual(assigned, [spec["id"] for spec in workloads(smoke)])
        self.assertEqual([len(items) for items in shards.values()], [9, 3, 2, 6])
        with (
            patch("run.workloads", side_effect=[workloads(), workloads(True)[:-1]]),
            self.assertRaisesRegex(ValueError, "partition"),
        ):
            load_shards()

    def test_shard_manifest_rejects_invalid_partitions_and_identifiers(self):
        original = json.loads((ROOT / "benchmarks/shards.json").read_text())
        for case in (
            "version",
            "sdk",
            "empty",
            "duplicate-shard",
            "unsafe-id",
            "empty-workloads",
            "duplicate-within",
            "duplicate-across",
            "missing",
            "unknown",
            "extra-field",
            "invalid-workload",
        ):
            manifest = copy.deepcopy(original)
            shards = manifest["shards"]
            if case == "version":
                manifest["manifest_version"] = True
            elif case == "sdk":
                manifest["sdk"] = "posthog-node"
            elif case == "empty":
                manifest["shards"] = []
            elif case == "duplicate-shard":
                shards[1]["id"] = shards[0]["id"]
            elif case == "unsafe-id":
                shards[0]["id"] = "../flags-single"
            elif case == "empty-workloads":
                shards[0]["workloads"] = []
            elif case == "duplicate-within":
                shards[0]["workloads"].append(shards[0]["workloads"][0])
            elif case == "duplicate-across":
                shards[1]["workloads"].append(shards[0]["workloads"][0])
            elif case == "missing":
                shards[0]["workloads"].pop()
            elif case == "unknown":
                shards[0]["workloads"][0] = "flags.unknown"
            elif case == "extra-field":
                manifest["extra"] = 1
            elif case == "invalid-workload":
                shards[0]["workloads"][0] = []
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "shards.json"
                path.write_text(json.dumps(manifest))
                with self.assertRaises(ValueError):
                    load_shards(path)

    def test_shard_and_filter_cli_errors_precede_execution(self):
        for selection in (
            ["--shard", "unknown"],
            ["--shard", "capture", "--filter", "capture."],
            ["--shard", "capture", "--filter", ""],
            ["--filter", "unknown"],
        ):
            with (
                self.subTest(selection=selection),
                patch("run.document_for") as describe,
                patch("sys.stderr", new_callable=io.StringIO),
                self.assertRaises(SystemExit) as error,
            ):
                main(
                    [
                        "--base",
                        str(ROOT),
                        "--head",
                        str(ROOT),
                        "--output",
                        "/unused",
                        *selection,
                    ]
                )
            self.assertEqual(error.exception.code, 2)
            describe.assert_not_called()

    def test_shards_preserve_suite_and_complete_alternating_repetitions(self):
        suites = []
        pair_ids = []
        for shard_id, selected in load_shards().items():
            with (
                self.subTest(shard=shard_id),
                tempfile.TemporaryDirectory() as directory,
                patch("run.run_worker", return_value={"status": "ok"}) as worker,
                patch("run.append_sample") as append,
                patch("sys.stderr", new_callable=io.StringIO),
            ):
                self.assertEqual(
                    main(
                        [
                            "--base",
                            str(ROOT),
                            "--head",
                            str(ROOT),
                            "--output",
                            directory,
                            "--smoke",
                            "--shard",
                            shard_id,
                        ]
                    ),
                    0,
                )
                base = json.loads((Path(directory) / "base.json").read_text())
                head = json.loads((Path(directory) / "head.json").read_text())
                self.assertEqual(base["suite"], head["suite"])
                suites.append(head["suite"])
                self.assertEqual(head["suite"]["repetitions"], 6)
                self.assertEqual(
                    base["execution"]["pair_id"], head["execution"]["pair_id"]
                )
                pair_ids.append(head["execution"]["pair_id"])
                for document in (base, head):
                    self.assertEqual(document["execution"]["shard_id"], shard_id)
                    self.assertEqual(
                        [b["id"] for b in document["benchmarks"]], selected
                    )
                specs = [spec for spec in workloads(True) if spec["id"] in selected]
                self.assertEqual(
                    [(call.args[1], call.args[2]) for call in worker.call_args_list],
                    [
                        (spec, measurement_pass)
                        for spec in specs
                        for _ in range(6)
                        for measurement_pass in spec["passes"]
                        for _ in range(2)
                    ],
                )
                expected = [
                    (spec["id"], repetition, role)
                    for spec in specs
                    for repetition in range(6)
                    for _ in spec["passes"]
                    for role in (
                        ["head", "base"] if repetition % 2 else ["base", "head"]
                    )
                ]
                # Record identity distinguishes the two sides even for same-revision runs.
                records = {
                    id(call.args[0]): call.args[0] for call in append.call_args_list
                }
                roles: dict[int, str] = {}
                for spec in specs:
                    matching = [
                        key
                        for key, record in records.items()
                        if record["id"] == spec["id"]
                    ]
                    roles.update(zip(matching, ("base", "head")))
                self.assertEqual(
                    [
                        (call.args[0]["id"], call.args[2], roles[id(call.args[0])])
                        for call in append.call_args_list
                    ],
                    expected,
                )
        self.assertTrue(all(suite == suites[0] for suite in suites))
        self.assertEqual(len(set(pair_ids)), 4)

    def test_capture_phase_reset_flush_schedules_and_fixture_ownership(self):
        for strategy in ("steady", "burst"):
            calls: list[int] = []
            submitted: list[dict[str, Any]] = []

            class Observed(Posthog):
                def capture(self, event, _submitted=submitted, **kwargs):
                    _submitted.append(kwargs["properties"])
                    return super().capture(event, **kwargs)

                def flush(self, timeout_seconds=10, _calls=calls, _submitted=submitted):
                    _calls.append(len(_submitted))
                    return super().flush(timeout_seconds=timeout_seconds)

            spec = next(
                s for s in workloads(True) if s["id"] == f"capture.drain.{strategy}.32"
            )
            with self.subTest(strategy=strategy), BenchmarkServer(spec) as server:
                workload = Workload(spec, server.url, client_class=Observed)
                try:
                    workload.prepare_phase(160)
                    workload.run(160)
                    workload.verify()
                    old_properties = list(submitted)
                    workload.prepare_phase(spec["operations"])
                    self.assertEqual(workload.state()["delivered"], 0)
                    calls.clear()
                    submitted.clear()
                    checkpoints: list[tuple[int, list[int]]] = []
                    with patch(
                        "gc.collect",
                        side_effect=AssertionError("GC inside measured work"),
                    ):
                        workload.run(
                            checkpoint=lambda n, points=checkpoints, flushes=calls: (
                                points.append((n, list(flushes)))
                            )
                        )
                    self.assertEqual(
                        calls, [100, 200] if strategy == "steady" else [200]
                    )
                    self.assertEqual(
                        checkpoints,
                        [(100, [100]), (200, [100, 200])]
                        if strategy == "steady"
                        else [(100, []), (200, [])],
                    )
                    count = len(calls)
                    state = workload.verify()
                    self.assertEqual(len(calls), count)
                    self.assertEqual(state["unique_sequences"], 200)
                    self.assertEqual(state["missing_sequences"], 0)
                    self.assertEqual(
                        [p["sequence"] for p in submitted], list(range(200))
                    )
                    self.assertEqual(
                        len({id(p) for p in submitted + old_properties}), 360
                    )
                    self.assertEqual(
                        [p["sequence"] for p in old_properties], list(range(160))
                    )
                finally:
                    workload.close()

    def test_balanced_duplicate_loss_fails_and_delivery_order_is_not_required(self):
        spec = next(s for s in workloads(True) if s["id"] == "capture.drain.burst.32")
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate), BenchmarkServer(spec) as server:
                workload = Workload(spec, server.url)
                try:
                    events = workload.events
                    if duplicate:
                        # Replace the final 16 events with the preceding 16: total and
                        # per-identity counts stay identical, but sequences do not.
                        events = events[:-16] + events[-32:-16]
                    control(server.url, "/batch", {"batch": list(reversed(events))})
                    workload.captured = spec["operations"]
                    for event in workload.events:
                        workload.captured_by_user[event["distinct_id"]] += 1
                    self.assertEqual(
                        workload.state()["delivered_by_user"], workload.captured_by_user
                    )
                    if duplicate:
                        with self.assertRaises(AssertionError):
                            workload.verify()
                        self.assertEqual(workload.state()["missing_sequences"], 16)
                    else:
                        workload.verify()
                finally:
                    workload.close()

    def test_incomplete_delivery_is_not_rescued_by_verification(self):
        calls = []

        class Incomplete(Posthog):
            def flush(self, timeout_seconds=10):
                calls.append(timeout_seconds)

        spec = next(s for s in workloads(True) if s["id"] == "capture.drain.burst.32")
        with BenchmarkServer(spec) as server:
            workload = Workload(spec, server.url, client_class=Incomplete)
            try:
                workload.run()
                with self.assertRaises(AssertionError):
                    workload.verify()
                self.assertEqual(calls, [None])
            finally:
                Posthog.flush(workload.client, timeout_seconds=None)
                workload.close()

    def test_refresh_generation_reset_and_full_sweeps_outside_measurement(self):
        spec = next(s for s in workloads(True) if s["mode"] == "refresh")
        with BenchmarkServer(spec) as server:
            workload = Workload(spec, server.url)
            try:
                workload.run(2)
                workload.verify()
                workload.prepare_phase(spec["operations"])
                self.assertEqual(workload.generation, 0)
                for generation in range(1, 5):
                    workload.run(1)
                    self.assertEqual(workload.generation, generation)
                    workload.verify()
                workload.prepare_phase(spec["operations"])
                checks = workload.checks
                checkpoints: list[int] = []
                with patch(
                    "gc.collect", side_effect=AssertionError("GC inside measured work")
                ):
                    workload.run(checkpoint=checkpoints.append)
                self.assertEqual(workload.checks, checks)
                self.assertEqual(checkpoints, [1, 2, 3, 4])
                workload.verify()
                self.assertEqual(workload.checks, checks + 16)
            finally:
                workload.close()

    def test_memory_checkpoint_positions_and_settling_order(self):
        for workload_id, operations, positions in (
            ("flags.load.1000", 70, [32, 64, 70]),
            ("flags.refresh.1000", 4, [1, 2, 3, 4]),
            ("capture.drain.steady.32", 250, [100, 200, 250]),
            ("capture.drain.burst.32", 250, [100, 200, 250]),
        ):
            spec = {
                **next(s for s in workloads(True) if s["id"] == workload_id),
                "operations": operations,
                "pass": "memory",
            }
            events: list[str] = []

            class Observed(Workload):
                def run(self, *args, _events=events, **kwargs):
                    _events.append("run")
                    with patch(
                        "gc.collect",
                        side_effect=AssertionError("GC inside measured work"),
                    ):
                        return super().run(*args, **kwargs)

                def verify(self, _events=events):
                    _events.append("verify")
                    return super().verify()

            with (
                self.subTest(workload=workload_id),
                BenchmarkServer(spec) as server,
                patch("worker.Workload", Observed),
                patch(
                    "worker.settle", side_effect=lambda log=events: log.append("settle")
                ),
                patch(
                    "worker.memory",
                    side_effect=lambda log=events: log.append("memory") or memory(),
                ),
            ):
                raw = execute(spec, server.url)
            self.assertEqual([p["operations"] for p in raw["checkpoints"]], positions)
            self.assertEqual(events[-4:], ["memory", "settle", "memory", "verify"])
            self.assertEqual(events.count("settle"), 2)

    def test_deterministic_and_changing_fixtures(self):
        self.assertEqual(digest(definitions(1000)), digest(definitions(1000)))
        self.assertNotEqual(digest(definitions(1000)), digest(definitions(1000, 1)))
        self.assertTrue(expected_flags(10, USERS[0])["boolean-0"])
        self.assertFalse(expected_flags(10, USERS[0], 1)["boolean-0"])
        self.assertFalse(expected_flags(10, USERS[0], 1)["cohort-5"])

    def test_public_entry_fixtures_and_lifecycle(self):
        for spec in workloads(smoke=True):
            with self.subTest(workload=spec["id"]), BenchmarkServer(spec) as server:
                workload = Workload(spec, server.url)
                try:
                    workload.run()
                    counts = workload.verify()
                    self.assertEqual(counts["unexpected"], [])
                    if spec["mode"] == "refresh":
                        self.assertEqual(counts["definitions"], spec["operations"] + 1)
                    if spec["mode"] in ("capture", "drain"):
                        self.assertEqual(counts["delivered"], spec["operations"])
                finally:
                    workload.close()
                self.assertFalse(
                    any(thread.is_alive() for thread in workload.client.consumers)
                )
                if workload.client.poller:
                    self.assertFalse(workload.client.poller.is_alive())

    def test_remote_fallback_is_not_a_successful_fake_evaluation(self):
        with BenchmarkServer(workloads(True)[0]) as server:
            with self.assertRaises(HTTPError):
                control(server.url, "/flags/?v=2", {})
            self.assertEqual(
                control(server.url, "/__benchmark/state")["unexpected"], ["/flags"]
            )

    def test_capture_checks_payload_and_identity_distribution(self):
        for mutation in ("payload", "identity"):

            class Corrupted(Posthog):
                def capture(self, event, _mutation=mutation, **kwargs):
                    if _mutation == "payload":
                        kwargs["properties"] = {**kwargs["properties"], "payload": ""}
                    else:
                        kwargs["distinct_id"] = USERS[0]["distinct_id"]
                    return super().capture(event, **kwargs)

            spec = next(s for s in workloads(True) if s["id"] == "capture.enqueue.4096")
            with self.subTest(mutation=mutation), BenchmarkServer(spec) as server:
                workload = Workload(spec, server.url, client_class=Corrupted)
                try:
                    workload.run()
                    with self.assertRaises(AssertionError):
                        workload.verify()
                    self.assertEqual(workload.state()["delivered"], spec["operations"])
                finally:
                    workload.close()

    def test_delayed_draining_finishes_inside_the_measurement(self):
        calls = []

        class ShortDeadline(Posthog):
            def flush(self, timeout_seconds=10):
                calls.append(timeout_seconds)
                # Make finite-deadline misuse observable without a five-second test.
                return super().flush(
                    timeout_seconds=None if timeout_seconds is None else 0.001
                )

        spec = {
            **next(s for s in workloads(True) if s["id"] == "capture.drain.steady.32"),
            "operations": 100,
        }
        with BenchmarkServer(spec, batch_delay=0.05) as server:
            workload = Workload(spec, server.url, client_class=ShortDeadline)
            try:
                measured = time_operations(workload.run, spec["operations"])
                self.assertGreaterEqual(measured["elapsed_ns"], 50_000_000)
                self.assertEqual(calls, [None])
                self.assertEqual(workload.verify()["delivered"], 100)
                self.assertEqual(
                    calls,
                    [None],
                    "Verification must not rescue an unfinished measured drain",
                )
            finally:
                workload.close()

    def test_preparation_forces_a_source_rebuild_and_preserves_runtime_versions(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("prepare.subprocess.run") as run,
            patch(
                "prepare.subprocess.check_output",
                return_value=b'{"posthog":"1.0","requests":"2.0"}',
            ),
        ):
            prepare(Path(directory), "3.12.12", "uv")
            command = run.call_args_list[0].args[0]
            self.assertEqual(
                command[command.index("--reinstall-package") + 1], "posthog"
            )
            self.assertIn("--frozen", command)
            self.assertIn("--no-editable", command)
            self.assertIn("--compile-bytecode", command)
            self.assertIn("--compile-bytecode", run.call_args_list[1].args[0])
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("prepare.subprocess.run"),
            patch(
                "prepare.subprocess.check_output",
                side_effect=[b'{"requests":"2.0"}', b'{"requests":"3.0"}'],
            ),
            self.assertRaisesRegex(RuntimeError, "changed locked SDK dependencies"),
        ):
            prepare(Path(directory), "3.12.12", "uv")

    def test_allocation_churn_includes_freed_small_python_allocations(self):
        def churn():
            for _ in range(20000):
                value = bytearray(200)
                del value
            gc.collect()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "default.bin"
            with memray.Tracker(path):
                churn()
            self.assertEqual(allocation_totals(path)["allocation_calls"], 0)
        measured = sample_allocations(churn)
        # Includes allocator requests even when objects have already been released.
        self.assertGreater(measured["allocated_bytes"], 20000 * 200)
        self.assertGreater(measured["allocation_calls"], 40000)
        self.assertLess(measured["allocation_calls"], 200000)

    def test_cpu_and_memory_controls(self):
        def busy():
            start = time.process_time_ns()
            while time.process_time_ns() - start < 25_000_000:
                pass

        measured = time_operations(busy, 1)
        self.assertGreaterEqual(measured["process_cpu_time_per_op"], 25_000_000)
        settle()
        before = memory()["rss"]
        retained = bytearray(64 * 1024 * 1024)
        for i in range(0, len(retained), 4096):
            retained[i] = 1
        settle()
        self.assertGreater(memory()["rss"] - before, 32 * 1024 * 1024)
        del retained
        settle()

    def test_refresh_checkpoints_detect_retained_state(self):
        class Retaining(Posthog):
            def __init__(self, *args, **kwargs):
                self.retained = []
                super().__init__(*args, **kwargs)

            def load_feature_flags(self):
                result = super().load_feature_flags()
                block = bytearray(8 * 1024 * 1024)
                for i in range(0, len(block), 4096):
                    block[i] = 1
                self.retained.append(block)
                return result

        spec = {
            **next(s for s in workloads(True) if s["mode"] == "refresh"),
            "operations": 4,
        }
        with BenchmarkServer(spec) as server:
            workload = Workload(spec, server.url, client_class=Retaining)
            try:
                settle()
                before = memory()["rss"]
                checkpoints = []

                def checkpoint(completed):
                    checkpoints.append((completed, memory()["rss"]))

                workload.run(checkpoint=checkpoint)
                workload.verify()
                self.assertEqual([point[0] for point in checkpoints], [1, 2, 3, 4])
                self.assertGreater(checkpoints[-1][1] - before, 24 * 1024 * 1024)
                self.assertGreater(
                    checkpoints[-1][1] - checkpoints[0][1], 16 * 1024 * 1024
                )
            finally:
                workload.close()

    def test_public_entry_cpu_and_allocation_regressions(self):
        class Expensive(Posthog):
            def get_feature_flag(self, *args, **kwargs):
                result = super().get_feature_flag(*args, **kwargs)
                start = time.thread_time_ns()
                while time.thread_time_ns() - start < 500_000:
                    pass
                allocation = bytearray(256 * 1024)
                allocation[0] = 1
                return result

        spec = workloads(True)[0]
        results = []
        for cls in (Posthog, Expensive):
            with BenchmarkServer(spec) as server:
                workload = Workload(spec, server.url, client_class=cls)
                try:
                    workload.run()
                    timing = time_operations(workload.run, spec["operations"])
                    allocation = sample_allocations(workload.run)
                    workload.verify()
                    results.append((timing, allocation))
                finally:
                    workload.close()
        self.assertGreater(
            results[1][0]["process_cpu_time_per_op"],
            results[0][0]["process_cpu_time_per_op"] * 3,
        )
        self.assertGreater(
            results[1][1]["allocated_bytes"], results[0][1]["allocated_bytes"] * 3
        )

    def test_worker_pass_isolation_and_installed_package(self):
        raw = run_worker(PYTHON, workloads(True)[0], "allocation")
        self.assertEqual(raw["status"], "ok", raw["stderr"])
        self.assertNotIn("elapsed_time_per_op", raw)
        self.assertTrue(Path(raw["entry"]).is_relative_to(ROOT / ".venv-benchmark"))
        result: dict[str, Any] = {"status": "ok", "metrics": [], "runs": []}
        append_sample(result, raw, 0)
        self.assertEqual(result["metrics"][1]["id"], "allocations_per_op")
        self.assertEqual(result["metrics"][1]["quality"], "measured")

    def test_worker_failures_are_not_zero_cost_samples(self):
        raw = run_worker(PYTHON, {**workloads(True)[0], "flag_index": 100}, "timing")
        self.assertEqual(raw["status"], "error")
        result: dict[str, Any] = {"status": "ok", "metrics": [], "runs": []}
        append_sample(result, raw, 0)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["metrics"], [])
        self.assertEqual(
            run_worker(PYTHON, workloads(True)[0], "timing", timeout=0.01)["status"],
            "timeout",
        )
        limited = run_worker(PYTHON, workloads(True)[0], "timing", rss_limit=1)
        self.assertTrue(limited["rss_limit_exceeded"])
        self.assertEqual(limited["status"], "error")

    def test_killed_worker_temporary_files_are_removed_by_the_coordinator(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Path(directory) / "worker.py"
            worker.write_text(
                "import tempfile, time\nprint(tempfile.mkdtemp(prefix='profile-'), flush=True)\ntime.sleep(10)\n"
            )
            with patch("run.WORKER", worker):
                raw = run_worker(PYTHON, workloads(True)[0], "allocation", timeout=0.5)
            self.assertEqual(raw["status"], "timeout")
            profile = Path(raw["stdout"].strip())
            self.assertTrue(profile.name.startswith("profile-"))
            self.assertTrue(profile.parent.name.startswith("posthog-benchmark-worker-"))
            self.assertFalse(profile.parent.exists())

    def test_repeated_timeouts_stop_the_suite_with_partial_results(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "run.run_worker", return_value={"status": "timeout", "pass": "timing"}
            ) as worker,
        ):
            code = main(
                [
                    "--base",
                    str(ROOT),
                    "--head",
                    str(ROOT),
                    "--output",
                    directory,
                    "--smoke",
                ]
            )
            self.assertEqual(code, 1)
            self.assertEqual(worker.call_count, 2)
            for role in ("base", "head"):
                document = json.loads((Path(directory) / f"{role}.json").read_text())
                self.assertEqual(len(document["benchmarks"]), 1)
                self.assertEqual(document["benchmarks"][0]["status"], "timeout")
                self.assertEqual(document["benchmarks"][0]["metrics"], [])
                self.assertEqual(len(document["benchmarks"][0]["runs"]), 1)

    def test_oom_classification_requires_evidence(self):
        self.assertEqual(classify_exit(stderr="Killed"), "error")
        self.assertEqual(classify_exit(stderr="Traceback...\nMemoryError\n"), "oom")
        self.assertEqual(classify_exit(rss_limit_exceeded=True), "error")
        self.assertEqual(classify_exit(timed_out=True), "timeout")

    def test_package_identity_and_source_fingerprints(self):
        self.assertTrue(Path(environment()["entry"]).is_relative_to(Path(sys.prefix)))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "module.py"
            path.write_text("VALUE = 1")
            before = source_digest(directory)
            path.write_text("VALUE = 2")
            self.assertNotEqual(source_digest(directory), before)
            manifest = Path(directory) / "shards.json"
            manifest.write_text('{"manifest_version": 1}')
            before = source_digest(directory)
            manifest.write_text('{"manifest_version": 2}')
            self.assertNotEqual(source_digest(directory), before)

    def test_paired_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                main(
                    [
                        "--base",
                        str(ROOT),
                        "--head",
                        str(ROOT),
                        "--output",
                        directory,
                        "--smoke",
                        "--repetitions",
                        "2",
                        "--filter",
                        "flags.single.boolean",
                    ]
                ),
                0,
            )
            base = json.loads((Path(directory) / "base.json").read_text())
            head = json.loads((Path(directory) / "head.json").read_text())
            self.assertEqual(base["execution"]["pair_id"], head["execution"]["pair_id"])
            self.assertEqual(
                base["execution"]["build_digest"], head["execution"]["build_digest"]
            )
            self.assertEqual(
                base["benchmarks"][0]["fixture_digest"],
                head["benchmarks"][0]["fixture_digest"],
            )
            self.assertEqual(base["suite"]["version"], 2)
            self.assertEqual(base["suite"]["specification"], "sdk-benchmarks/v1")
            self.assertEqual(base["revision"]["role"], "base")
            self.assertEqual(head["revision"]["role"], "head")
            for metric in head["benchmarks"][0]["metrics"]:
                self.assertEqual(len(metric["samples"]), 2)

    def test_worker_describes_the_installed_distribution(self):
        # Validate the actual recorded installation, not source-tree import resolution.
        output = subprocess.check_output(
            [PYTHON, "-B", str(ROOT / "benchmarks/worker.py"), "--describe"],
            cwd=tempfile.gettempdir(),
        )
        self.assertEqual(
            json.loads(output)["build_digest"], environment()["build_digest"]
        )


if __name__ == "__main__":
    unittest.main()
