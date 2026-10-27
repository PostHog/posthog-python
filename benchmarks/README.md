# Python SDK benchmarks

PR-only, paired measurements of independently installed Python SDK revisions. Each workload/pass/repetition runs in a fresh process against the target's non-editable wheel. Benchmark code comes from one checkout and emits `base.json` and `head.json` in the Node pilot's result structure.

## Setup and local execution

Use **CPython 3.12.12** and **uv 0.13.4**, matching `.github/workflows/benchmarks.yml`.

From the checkout containing this suite, prepare each revision:

```sh
python benchmarks/prepare.py /absolute/path/to/head
python benchmarks/prepare.py /absolute/path/to/base
```

Each target gets its own `.venv-benchmark`. Preparation installs the SDK and runtime dependencies from that revision's frozen `uv.lock`, builds a non-editable SDK wheel, then installs hash-pinned benchmark instrumentation from this suite's `requirements.txt`. Preparation fails if instrumentation changes an already installed SDK dependency version. Both environments precompile bytecode: importing cached bytecode on one side and compiling source on the other can otherwise create an apparent RSS regression even with identical SDK source. Workers do not write new bytecode. The benchmark environment is disposable and separate from the ordinary development `.venv`.

Run the controls and benchmarks using the head environment:

```sh
.venv-benchmark/bin/python -B benchmarks/test_benchmarks.py
.venv-benchmark/bin/python -B benchmarks/run.py \
  --base /absolute/path/to/base \
  --head /absolute/path/to/head \
  --output /tmp/python-benchmark-results \
  --repetitions 6
```

`--smoke --repetitions 1` checks integration quickly; smoke results are not suitable for performance conclusions. `--filter flags.all.` selects workload IDs by prefix. Using the same revision on both sides measures repeatability, not an improvement.

Rerun preparation after changing SDK source. Preparation forces a `posthog` rebuild/reinstall so source-only changes cannot reuse uv's cached wheel. The worker deliberately does not import the repository source tree. Results fingerprint the installed package's Python files and record its entry path, SDK commit/dirty state, package version, lockfile digest, actual installed dependency/tool versions, benchmark source digest, runtime, fixture digest, and paired-run ID.

To update benchmark tooling, use a supported uv version and review the regenerated lock:

```sh
uv pip compile benchmarks/requirements.in --python-version 3.12 --universal \
  --generate-hashes --exclude-newer '7 days' --output-file benchmarks/requirements.txt
```

## Workloads

| Workload | Public path | Measurements |
| --- | --- | --- |
| Nine single-flag cases | `get_feature_flag` | Elapsed time/op, process CPU/op, Python allocator bytes/calls per op |
| All flags: 10, 100, 1,000 | `get_all_flags` | Same metrics per complete all-flags call |
| Sustained 1,000-flag evaluation | Eight threads calling `get_all_flags` | RSS observations and post-settling RSS |
| Forty definition refreshes | `load_feature_flags`, then validated `get_all_flags` | Memory checkpoints every five cycles and at the end |
| Capture: 32-byte and 4,096-byte string properties | `capture` | Foreground elapsed time and caller-thread CPU per event |
| Capture and drain: both payload sizes | `capture`, `flush` every 100 events | Full-pipeline time, process CPU, allocator bytes/calls, and memory |

The flag mix and sixteen identities match the Node pilot conceptually: boolean, percentage rollout, multivariate assignment, person properties, regex, cohort, early/late/no-match, and inactive flags. Expected values are computed independently of the SDK. Every identity is validated before and after measurement. Refresh generations alter actual boolean/cohort results and regex patterns so a fetch without a state update fails validation.

The suite covers the supported legacy flag getters, not `evaluate_flags` snapshots. Deprecation warnings are encountered during setup. Eight threads are **GIL-constrained concurrency**, not a claim of eight-way parallel Python execution.

### Transport and lifecycle

Python has no public fetch adapter. A bounded loopback HTTP server runs in the coordinator process, outside the measured worker, and serves definitions and accepts uncompressed v0 batches. Its routes are limited to the fixture protocol and a control/inspection interface. It retains counters and per-identity counts, not event history. Remote flag fallback is unexpected and fails verification.

The SDK uses real requests, JSON parsing/serialization, background consumers, flag loading, flush, and shutdown. No private queue, evaluator, or poller is replaced. Automatic polling and partial-batch deadlines are set to one hour, longer than the worker timeout. Refreshes and draining use public methods. Worker shutdown must stop SDK threads.

Capture checks require the correct event name, exact payload, identity distribution, and total delivered count. Rejected captures, timeouts that leave undelivered events, malformed payloads, and remote fallback cannot appear as successful low-cost work.

### Capture measurement boundaries

`capture()` returns after foreground preparation/enqueue, while a consumer can concurrently prepare its batch. There is no public completion boundary for that background preparation short of flushing. Therefore:

- `capture.enqueue.*` reports foreground elapsed time and **caller-thread CPU**, not process-wide CPU or allocation totals.
- `capture.drain.*` includes `flush(timeout_seconds=None)` and reports the complete delivery path's process CPU and allocations, including requests/serialization. The external worker timeout bounds this wait. Verification does not flush again: unfinished measured work cannot be rescued outside the measurement. Server CPU and server allocations are excluded; elapsed time includes loopback transport.
- Enqueue-only allocation metrics are explicitly unavailable and direct readers to the drain workload.

The enqueue workload uses 1,000 events, below queue/batch-count limits; partial batches can still be prepared by the consumer. These metrics describe their named paths rather than pretending Python and Node have identical asynchronous lifecycles.

## Measurement methods

Base/head alternate first position between independent repetitions and never benchmark concurrently. The same head-side fixtures and measurement code run against both installations. The runner verifies matching Python, profiler, psutil, OS, and architecture versions. Both workers use a fixed hash seed and a clean Python configuration environment.

Warmup and correctness checks happen outside timing. Timing, allocation profiling, and memory observation are separate passes. Allocation passes use smaller fixed operation counts where tracing every Python allocation would otherwise produce excessive profiler files; the actual count is included in each result and used as its denominator. These counts are identical for base and head.

### Time and CPU

- `elapsed_time_per_op`: `time.perf_counter_ns`, normalized over a batch.
- `process_cpu_time_per_op`: `time.process_time_ns`, including all worker threads.
- `caller_thread_cpu_time_per_op`: `time.thread_time_ns`, for foreground enqueue only.

The metrics include the small benchmark-loop overhead. Elapsed time is not CPU utilization.

### Allocations

Memray runs with `trace_python_allocators=True` and `FileFormat.ALL_ALLOCATIONS`. After tracking stops, the adapter streams all records and sums `PYMALLOC_MALLOC`, `PYMALLOC_CALLOC`, and `PYMALLOC_REALLOC` requests. This includes requests whose allocations were already freed. It excludes free records and native backing allocations, avoiding interpreting backing arenas as individual Python allocations.

- `allocated_bytes_per_op` is cumulative **requested Python allocator bytes**. A reallocation contributes its requested size, not just its growth.
- `allocations_per_op` is **Python allocator calls, including reallocations**, not a count of distinct Python objects.
- Both are measured counters, unlike Node's estimated V8 allocation bytes. They are still not cross-language-comparable totals.
- Native-library allocations outside Python's allocators are not included. RSS covers the whole process.

Profiler capture and aggregation do not contribute to reported timing. Profiles are streamed and removed after aggregation so repeated runs do not retain large binary captures. Raw counters and profiler versions remain in the JSON.

The control suite demonstrates that this configuration records freed small allocations that default Memray tracking does not expose as Python allocator requests. Other controls preserve SDK outputs while adding CPU work and allocation churn; both must be detected. See [Memray's allocator documentation](https://bloomberg.github.io/memray/python_allocators.html) and [API documentation](https://bloomberg.github.io/memray/api.html).

### Memory

- Idle RSS: after imports, definitions, validation, warmup, and settling.
- Observed workload peak RSS: operation checkpoints and end-of-work observation; can miss intervening spikes.
- Lifetime peak RSS: `resource.getrusage`, normalized from platform-specific units. Includes setup and warmup, not just measured work.
- Post-settling RSS: after two `gc.collect()` calls separated by a scheduler yield. Python reference counting also frees objects during ordinary execution.

CPython has no uninstrumented total managed-heap byte counter equivalent to Node's `heapUsed`. `post_settling_heap_used` is explicitly unsupported; enabling tracing in the RSS pass would change the measurement. Retained RSS alone does not prove a leak because allocators can keep arenas resident. A public-entry control deliberately retains 8 MiB on each refresh and verifies that post-GC checkpoints expose the growth.

Each worker has a two-minute timeout and an externally monitored **512 MiB RSS ceiling**, checked approximately every 20 ms. This is a safety bound, not an exact OS-enforced limit: short spikes can exceed it between observations. A ceiling violation is reported as `error` with `rss_limit_exceeded`, not falsely described as a confirmed OS OOM. An explicit Python `MemoryError` is classified as OOM; a generic kill is not. The suite stops after two worker timeouts, after completing the current base/head pair, and preserves partial results for upload. Worker temporary files, including profiler traces, are owned and removed by the coordinator even when a worker is killed.

## Results and CI

The shared JSON format uses `schema_version: 1`, with the same top-level revision, suite, execution, benchmark, metric, sample, and status structure as Node. Measurement IDs, units, method names, and quality are explicit. Python's intentional differences are allocator-call counts, foreground-thread CPU for enqueue, unavailable uninstrumented heap size, and a separate HTTP receiver. The contract, semantic validation, comparisons, and rendering live in [PostHog/.github](https://github.com/PostHog/.github/blob/main/.github/workflows/sdk-benchmark-report.md).

The workflow runs for every pull request opened, reopened, or updated in `PostHog/posthog-python`. It builds the merge base and exact head into isolated environments, then runs this head-side suite against both. It requests read-only permissions, uses pinned actions, requires no service credentials, preserves partial results after worker failures, and invokes the SHA-pinned reporting action in `PostHog/.github` to render validated comparisons in the job summary. Raw JSON and rendered reports are uploaded as separate artifacts.

Correctness failures and broken execution fail the job. Performance differences do not: this producer does not compute regression verdicts. Comparisons expose descriptive medians and variability rather than calibrated regression verdicts. A separate `workflow_run` caller uses the SHA-pinned reusable workflow in `PostHog/.github` to verify source-run/artifact/PR identities and publish comments without executing PR code with write permissions. This caller must land on the default branch before automatic comments are active, including for fork PRs. Calibrate on the actual Linux runner before introducing performance gates; local macOS results are not a CI noise floor.
