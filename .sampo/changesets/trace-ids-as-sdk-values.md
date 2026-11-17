---
pypi/posthog: major
---

The `$trace_id` and `$span_id` that the SDK takes from the active OpenTelemetry span now fill after the call's `properties`, context tags and `super_properties`, and before `before_send`. A `$trace_id` or `$span_id` in a context tag or `super_properties` now replaces the span's IDs, and setting either key stops the SDK adding both. `AsyncPosthog` follows the same rules.
