---
pypi/posthog: patch
---

Support mixed user+group targeting in local flag evaluation. Flags with per-condition `aggregation_group_type_index` now resolve properties and bucketing per condition instead of using the flag-level aggregation only.
