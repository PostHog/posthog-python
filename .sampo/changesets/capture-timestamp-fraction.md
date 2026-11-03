---
pypi/posthog: patch
---

Capture timestamps always have six fractional-second digits, even on a whole second: `2026-01-02T03:04:05.000000+00:00`, not `2026-01-02T03:04:05+00:00`. This applies to the event `timestamp`, the batch `created_at` and the `PostHog-Request-Timestamp` header. The `timestamp` that `before_send` receives has the same format.
