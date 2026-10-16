---
pypi/posthog: minor
---

MCP analytics no longer sends standalone `$identify` events. The `identify` callback still sets `distinct_id`, `$set` and `$groups` on every event.
