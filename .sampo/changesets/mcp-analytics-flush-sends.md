---
pypi/posthog: minor
---

`McpAnalytics.flush()`, on the handle `instrument()` returns, now flushes the posthog client after it waits for in-flight MCP captures, so the captured events are sent. It never raises: a failed client flush is logged. On a per-request runtime, `await analytics.flush()` alone is enough; you no longer need a separate `posthog.flush()`.
