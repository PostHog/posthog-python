"""Run the head-side suite against two independently installed SDK revisions."""

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import psutil
from fixtures import USERS, definitions, digest, workloads
from identity import source_digest
from measurement import classify_exit
from server import BenchmarkServer

HERE = Path(__file__).resolve().parent
WORKER = HERE / "worker.py"
TIMEOUT = 120
MAX_WORKER_TIMEOUTS = 2
RSS_LIMIT = 512 * 1024 * 1024


def load_shards(path=HERE / "shards.json"):
    manifest = json.loads(path.read_text())
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"manifest_version", "sdk", "shards"}
        or type(manifest["manifest_version"]) is not int
        or manifest["manifest_version"] != 1
        or manifest["sdk"] != "posthog-python"
        or not isinstance(manifest["shards"], list)
        or not 1 <= len(manifest["shards"]) <= 16
    ):
        raise ValueError("Invalid shard manifest")
    shards = {}
    assigned = []
    for shard in manifest["shards"]:
        if (
            not isinstance(shard, dict)
            or set(shard) != {"id", "workloads"}
            or not isinstance(shard["id"], str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", shard["id"])
            or shard["id"] in shards
            or not isinstance(shard["workloads"], list)
            or not shard["workloads"]
            or not all(isinstance(item, str) for item in shard["workloads"])
        ):
            raise ValueError("Invalid or duplicate shard")
        shards[shard["id"]] = shard["workloads"]
        assigned.extend(shard["workloads"])
    for smoke in (False, True):
        inventory = [spec["id"] for spec in workloads(smoke)]
        if (
            len(set(assigned)) != len(assigned)
            or len(set(inventory)) != len(inventory)
            or set(assigned) != set(inventory)
        ):
            raise ValueError(
                "Shard workloads must exactly partition full and smoke inventories"
            )
    return shards


def worker_env():
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("PYTHON")
    }
    env.update(
        PYTHONHASHSEED="0",
        PYTHONNOUSERSITE="1",
        PYTHONDONTWRITEBYTECODE="1",
        POSTHOG_CAPTURE_MODE="v0",
    )
    return env


def run_worker(python, spec, measurement_pass, timeout=TIMEOUT, rss_limit=RSS_LIMIT):
    with (
        tempfile.TemporaryDirectory(prefix="posthog-benchmark-worker-") as directory,
        BenchmarkServer(spec) as server,
    ):
        output = Path(directory) / "result.json"
        command = [
            str(python),
            "-B",
            str(WORKER),
            json.dumps({**spec, "pass": measurement_pass}),
            server.url,
            str(output),
        ]
        with subprocess.Popen(
            command,
            cwd=directory,
            env={**worker_env(), "TMPDIR": directory},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            logs = {"stdout": bytearray(), "stderr": bytearray()}

            def read_log(stream, target):
                while chunk := stream.read(4096):
                    target.extend(chunk)
                    del target[:-65536]

            readers = [
                threading.Thread(
                    target=read_log,
                    args=(getattr(process, name), logs[name]),
                    daemon=True,
                )
                for name in logs
            ]
            for reader in readers:
                reader.start()
            observed = psutil.Process(process.pid)
            start = time.monotonic()
            timed_out = limit_exceeded = False
            while process.poll() is None:
                try:
                    limit_exceeded = observed.memory_info().rss > rss_limit
                except psutil.NoSuchProcess:
                    pass
                timed_out = time.monotonic() - start > timeout
                if timed_out or limit_exceeded:
                    process.kill()
                    break
                time.sleep(0.02)
            process.wait()
            for reader in readers:
                reader.join()
            diagnostic = {
                name: data.decode("utf-8", errors="replace")
                for name, data in logs.items()
            }
            if (
                process.returncode == 0
                and output.exists()
                and not timed_out
                and not limit_exceeded
            ):
                if output.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError("Worker result exceeds size limit")
                return {"status": "ok", **json.loads(output.read_text()), **diagnostic}
            return {
                "status": classify_exit(
                    timed_out=timed_out,
                    rss_limit_exceeded=limit_exceeded,
                    stderr=diagnostic["stderr"],
                ),
                "code": process.returncode,
                "pass": measurement_pass,
                "rss_limit_exceeded": limit_exceeded,
                "error": "Worker exceeded RSS limit"
                if limit_exceeded
                else "Worker did not complete",
                **diagnostic,
            }


def git(directory, *args):
    return subprocess.check_output(
        ["git", "-C", str(directory), *args], text=True
    ).strip()


def document_for(directory, role, pair_id, suite):
    python = directory / ".venv-benchmark/bin/python"
    with tempfile.TemporaryDirectory(prefix="posthog-benchmark-identity-") as cwd:
        execution = json.loads(
            subprocess.check_output(
                [python, "-B", WORKER, "--describe"],
                cwd=cwd,
                env=worker_env(),
                text=True,
                timeout=TIMEOUT,
            )
        )
    execution.update(
        {
            "pair_id": pair_id,
            "runtime_flags": ["-B"],
            "python_hash_seed": 0,
            "bytecode_policy": "uv precompiles both environments; workers do not write bytecode",
            "runner_class": "github-hosted-or-self-hosted"
            if os.getenv("RUNNER_NAME")
            else "local",
            "runner_image": os.getenv("ImageVersion"),
            "dependency_lock_digest": digest((directory / "uv.lock").read_bytes()),
            "resource_limits": {
                "process_rss_limit_bytes": RSS_LIMIT,
                "worker_timeout_ms": TIMEOUT * 1000,
                "max_worker_timeouts": MAX_WORKER_TIMEOUTS,
            },
        }
    )
    return {
        "schema_version": 1,
        "sdk": "posthog-python",
        "revision": {
            "role": role,
            "commit": git(directory, "rev-parse", "HEAD"),
            "dirty": bool(
                git(
                    directory,
                    "status",
                    "--porcelain",
                    "--",
                    ".",
                    ":(exclude).venv-benchmark",
                )
            ),
        },
        "suite": suite,
        "execution": execution,
        "benchmarks": [],
    }


def append_sample(benchmark, raw, repetition):
    benchmark["runs"].append({"repetition": repetition, **raw})
    if raw["status"] != "ok":
        benchmark["status"] = raw["status"]
        return

    def add(name, unit, method, value):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"Invalid {name}: {value}")
        metric = next((m for m in benchmark["metrics"] if m["id"] == name), None)
        if metric is None:
            metric = {
                "id": name,
                "unit": unit,
                "method": method,
                "quality": "measured",
                "direction": "lower",
                "status": "ok",
                "samples": [],
            }
            benchmark["metrics"].append(metric)
        metric["samples"].append(
            {
                "repetition": repetition,
                "value": value,
                "operations": raw["operations"],
                **({"elapsed_ns": raw["elapsed_ns"]} if "elapsed_ns" in raw else {}),
            }
        )

    def unavailable(name, unit, reason):
        if not any(m["id"] == name for m in benchmark["metrics"]):
            benchmark["metrics"].append(
                {"id": name, "unit": unit, "status": "unsupported", "reason": reason}
            )

    if raw["pass"] == "timing":
        add(
            "elapsed_time_per_op",
            "ns/op",
            "time.perf_counter_ns",
            raw["elapsed_time_per_op"],
        )
        if "process_cpu_time_per_op" in raw:
            add(
                "process_cpu_time_per_op",
                "ns/op",
                "time.process_time_ns",
                raw["process_cpu_time_per_op"],
            )
        else:
            add(
                "caller_thread_cpu_time_per_op",
                "ns/op",
                "time.thread_time_ns",
                raw["caller_thread_cpu_time_per_op"],
            )
            for name, unit in (
                ("allocated_bytes_per_op", "bytes/op"),
                ("allocations_per_op", "count/op"),
            ):
                unavailable(
                    name,
                    unit,
                    "Background preparation has no public completion boundary before flush; see capture.drain for full-pipeline allocations.",
                )
    elif raw["pass"] == "allocation":
        add(
            "allocated_bytes_per_op",
            "bytes/op",
            "memray.python_allocator_requests",
            raw["allocated_bytes"] / raw["operations"],
        )
        add(
            "allocations_per_op",
            "count/op",
            "memray.python_allocator_requests",
            raw["allocation_calls"] / raw["operations"],
        )
    elif raw["pass"] == "memory":
        add(
            "idle_process_rss",
            "bytes",
            "psutil.Process.memory_info.rss",
            raw["before"]["rss"],
        )
        add(
            "observed_workload_peak_rss",
            "bytes",
            "psutil.Process.memory_info.rss.operation_checkpoints",
            raw["observed_peak_rss"],
        )
        add(
            "process_lifetime_peak_rss",
            "bytes",
            "resource.getrusage.ru_maxrss",
            raw["process_lifetime_peak_rss"],
        )
        add(
            "post_settling_process_rss",
            "bytes",
            "psutil.Process.memory_info.rss",
            raw["after_settling"]["rss"],
        )
        unavailable(
            "post_settling_heap_used",
            "bytes",
            "CPython has no uninstrumented total managed-heap byte counter; tracing would distort this RSS pass.",
        )


def main(args=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--head", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=6, choices=range(1, 31))
    parser.add_argument("--smoke", action="store_true")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--filter", default="")
    selection.add_argument("--shard")
    options = parser.parse_args(args)
    try:
        shards = load_shards()
    except ValueError as error:
        parser.error(str(error))
    if options.shard is not None and options.shard not in shards:
        parser.error(f"Unknown shard: {options.shard}")
    specs = [
        spec
        for spec in workloads(options.smoke)
        if (
            spec["id"] in shards[options.shard]
            if options.shard is not None
            else spec["id"].startswith(options.filter)
        )
    ]
    if not specs:
        parser.error("No matching workloads")
    pair_id = str(uuid.uuid4())
    suite = {
        "id": "python-sdk-performance",
        "version": 2,
        "specification": "sdk-benchmarks/v1",
        "source_commit": git(HERE, "rev-parse", "HEAD"),
        "source_digest": source_digest(HERE),
        "profile": "smoke" if options.smoke else "full",
        "repetitions": options.repetitions,
        "allocation_tracking": {
            "trace_python_allocators": True,
            "file_format": "ALL_ALLOCATIONS",
            "scope": "PYMALLOC_MALLOC/CALLOC/REALLOC requests; all worker threads",
        },
        "transport": "loopback HTTP receiver in coordinator process; uncompressed v0",
        "warmup": "two refreshes; otherwise min(operations, 32 for 1000 flags or 160); verified phase reset before measurement",
        "settling": "two gc.collect calls separated by sleep(0)",
        "memory_sampling": "operation checkpoints; lifetime high-water mark includes setup and warmup",
        "capture": {
            "flush_at": 100000,
            "flush_interval_seconds": 3600,
            "consumer_threads": 1,
            "max_queue_size": 100001,
            "compression": "none",
        },
        "flag_poll_interval_seconds": 3600,
    }
    directories = {"base": options.base.resolve(), "head": options.head.resolve()}
    documents = {
        role: document_for(directory, role, pair_id, suite)
        for role, directory in directories.items()
    }
    if options.shard is not None:
        for document in documents.values():
            document["execution"]["shard_id"] = options.shard
    for key in ("runtime_version", "measurement_tools", "os", "arch"):
        if documents["base"]["execution"][key] != documents["head"]["execution"][key]:
            raise ValueError(f"Base/head {key} differ")
    options.output.mkdir(parents=True, exist_ok=True)

    def save():
        for role, document in documents.items():
            temporary = options.output / f"{role}.json.tmp"
            temporary.write_text(json.dumps(document, indent=2, allow_nan=False) + "\n")
            temporary.replace(options.output / f"{role}.json")

    failures = 0
    timeouts = 0
    for spec in specs:
        records = {}
        for role in directories:
            records[role] = {
                "id": spec["id"],
                "parameters": spec,
                "fixture_digest": digest(
                    {
                        "spec": spec,
                        "definitions": definitions(spec["flag_count"])
                        if "flag_count" in spec
                        else None,
                        "users": USERS,
                    }
                ),
                "status": "ok",
                "metrics": [],
                "runs": [],
            }
            documents[role]["benchmarks"].append(records[role])
        for repetition in range(options.repetitions):
            for measurement_pass in spec["passes"]:
                for role in ["head", "base"] if repetition % 2 else ["base", "head"]:
                    raw = run_worker(
                        directories[role] / ".venv-benchmark/bin/python",
                        spec,
                        measurement_pass,
                    )
                    append_sample(records[role], raw, repetition)
                    failures += raw["status"] != "ok"
                    timeouts += raw["status"] == "timeout"
                    print(
                        f"{spec['id']} {measurement_pass} {repetition + 1}/{options.repetitions} {role}: {raw['status']}",
                        file=sys.stderr,
                    )
                    save()
                if timeouts >= MAX_WORKER_TIMEOUTS:
                    return 1
            if any(record["status"] != "ok" for record in records.values()):
                break
    return int(failures > 0)


if __name__ == "__main__":
    sys.exit(main())
