---
pypi/posthog: minor
---

Add an opt-in `capture_trace_context` client option. When enabled, and a valid OpenTelemetry span is active at capture time, its trace and span IDs are attached to events captured with `capture()` and `capture_ai()` as `$trace_id` and `$span_id`, so they can be correlated with backend traces. Disabled by default, and explicit `$trace_id`/`$span_id` properties take precedence.
