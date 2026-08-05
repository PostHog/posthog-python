---
pypi/posthog: patch
---

`alias()` now validates both identities before enqueuing. Previously `alias(None, "user-123")` (or an empty-string `previous_id`) sent a `$create_alias` event with a null/empty `distinct_id`, which cannot link anything and just adds an unusable event to the project. Missing identities are now dropped with a warning instead, matching the sdk-specs `alias` contract. The drop that already happened when no alias target could be resolved now logs a warning too, and a non-string `previous_id` such as `0` is stringified consistently in both `distinct_id` and `properties.distinct_id`.
