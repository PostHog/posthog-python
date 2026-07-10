---
pypi/posthog: minor
---

Internal restructure of the capture pipeline. No public API or default behavior changes: `capture()` batches and posts to `/batch/` exactly as before.

Notes for code that reaches into SDK internals:

- `Consumer` now takes `endpoint=` / `max_msg_size=` parameters instead of `dedicated_ai_endpoint`, and no longer special-cases `$ai_*` events.
- `Client.queue` and `Client.consumers` are now read-only properties (reads behave as before; assignment raises `AttributeError`).
- The internal `Posthog(..., _dedicated_ai_endpoint=...)` constructor kwarg is removed (it was never public).
