---
pypi/posthog: minor
---

Refactored capture internals to support multiple delivery lanes per client. Added an internal test lane for heavy AI events.

Events captured after `shutdown()` are now dropped with a warning instead of being silently queued with no consumer to deliver them.
