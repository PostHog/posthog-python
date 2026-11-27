---
pypi/posthog: patch
---

`capture_exception` (and `capture` with `capture_trace_context`) now adds `$trace_sampled` next to the active OpenTelemetry span's `$trace_id` and `$span_id`. An unsampled trace still has a valid trace ID, so the SDK attaches it, but the trace itself is never exported. `$trace_sampled` tells readers such as error tracking's trace tab whether a stored trace can exist before they look it up. Like the trace and span IDs, it is only added when the event, context tags and super properties set neither `$trace_id` nor `$span_id`.
