---
pypi/posthog: minor
---

Add opt-in, dependency-free macOS runtime metrics with `metrics={"capture_runtime_metrics": True}`. Process metrics follow OpenTelemetry conventions: cumulative `process.cpu.time` counters and CPU-normalized `process.cpu.utilization` gauges distinguished by `cpu.mode`, plus current RSS (`process.memory.usage`) and native threads (`process.thread.count`) as cumulative non-monotonic sums. `process.memory.utilization` reports RSS divided by total physical RAM as a gauge fraction, not utilization against a container limit. CPython GC collections, collected objects, and uncollectable objects use cumulative `gc.get_stats()` counters distinguished by `cpython.gc.generation`, not allocation bookkeeping or heap-size measurements.

Retain custom `app.process.cpu.percent` (100% means one busy core) and cumulative `app.process.cpu.ctx_switch.total` (the native reader provides a combined count, not OpenTelemetry's required voluntary/involuntary split). Runtime sums preserve the latest observation and a stable start time across flushes and retries; manual metrics retain their existing wire format and delta aggregation. Forked workers receive independent identities and CPU baselines. `shutdown()` collects a final sample, flushes, and stops collection. Unsupported platforms warn and do not collect; collection remains disabled by default.
