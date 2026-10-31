"""One isolated revision/workload/measurement-pass execution."""

import json
import sys
import time
from pathlib import Path

from measurement import (
    lifetime_peak_rss,
    memory,
    sample_allocations,
    settle,
    time_operations,
)
from workload import Workload


def execute(spec, host):
    assert sys.flags.optimize == 0, "Benchmark correctness checks require assertions"
    import posthog

    with_installed_sdk = Path(posthog.__file__).resolve()
    assert with_installed_sdk.is_relative_to(Path(sys.prefix)), (
        "Expected the target's installed wheel"
    )
    workload = Workload(spec, host)
    try:
        warmup = (
            2
            if spec["mode"] == "refresh"
            else min(spec["operations"], 32 if spec.get("flag_count") == 1000 else 160)
        )
        operations = (
            spec.get("allocation_operations", spec["operations"])
            if spec["pass"] == "allocation"
            else spec["operations"]
        )
        workload.prepare_phase(warmup)
        workload.run(warmup)
        workload.verify()
        workload.prepare_phase(operations)
        settle()
        result = {
            "pass": spec["pass"],
            "operations": operations,
            "warmup_operations": warmup,
            "entry": str(with_installed_sdk),
        }
        if spec["pass"] == "timing":
            result.update(time_operations(workload.run, spec["operations"]))
            if spec["mode"] == "capture":
                # Foreground enqueue does not have a public background-preparation completion signal.
                # Process-wide CPU/allocations for full delivery belong to the drain workload.
                del result["process_cpu_time_per_op"]
            else:
                del result["caller_thread_cpu_time_per_op"]
        elif spec["pass"] == "allocation":
            result.update(sample_allocations(lambda: workload.run(operations)))
        elif spec["pass"] == "memory":
            result["before"] = memory()
            result["checkpoints"] = []
            observed = result["before"]["rss"]

            def checkpoint(completed):
                nonlocal observed
                snapshot = memory()
                observed = max(observed, snapshot["rss"])
                result["checkpoints"].append({"operations": completed, **snapshot})

            start = time.perf_counter_ns()
            workload.run(checkpoint=checkpoint)
            result["elapsed_ns"] = time.perf_counter_ns() - start
            result["after_work"] = memory()
            result["observed_peak_rss"] = max(observed, result["after_work"]["rss"])
            result["process_lifetime_peak_rss"] = lifetime_peak_rss()
            settle()
            result["after_settling"] = memory()
        else:
            raise ValueError(f"Unknown pass {spec['pass']}")
        result["validation"] = workload.verify()
        return result
    finally:
        workload.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["--describe"]:
        from identity import environment

        print(json.dumps(environment()))
    else:
        spec, host, output = sys.argv[1:]
        Path(output).write_text(
            json.dumps(execute(json.loads(spec), host)), encoding="utf-8"
        )
