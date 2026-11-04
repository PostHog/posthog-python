import gc
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
from run import append_sample, main, run_worker
from server import BenchmarkServer
from workload import Workload, control

from posthog import Posthog

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv-benchmark/bin/python"


class BenchmarksTest(unittest.TestCase):
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
            **next(s for s in workloads(True) if s["id"] == "capture.drain.32"),
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
                    settle()
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
