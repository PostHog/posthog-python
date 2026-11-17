---
pypi/posthog: minor
---

`AsyncPosthog.capture_exception` now adds the active OpenTelemetry span's `$trace_id` and `$span_id`, like `Client.capture_exception`, even when `capture_trace_context` is off.
