---
pypi/posthog: patch
---

`group_identify()` now validates the group identity before enqueuing. Previously `group_identify("company", None)` (or an empty-string `group_type` / `group_key`) sent a `$groupidentify` event with a null/empty `$group_type` or `$group_key`, which cannot address a group profile and just adds an unusable event to the project. Missing values are now dropped with a warning instead, matching the sdk-specs `group-identify` contract. Valid values, including non-string group keys, are passed through unchanged.
