---
pypi/posthog: patch
---

Capture keeps the first event for each uuid in a request and drops the later copies. `on_error` now reports each dropped copy in `CaptureError.drops` as `(uuid, "duplicate_event_uuid")`, and the SDK does not resend it. `verdict_summary()` counts each drop by its own reason.
