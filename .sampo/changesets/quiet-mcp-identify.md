---
pypi/posthog: minor
---

MCP analytics no longer sends standalone `$identify` events, so insights, funnels, or cohorts built on MCP `$identify` stop receiving new data. The `identify` callback still sets `distinct_id`, `$set` and `$groups` on every event.
