---
pypi/posthog: patch
---

Honor the definitions snapshot's `property_matching_version` during local feature flag evaluation, including person, group, cohort, and flag dependency conditions. Version 2 uses explicit boolean equality; missing/1 retains legacy truthiness. Preserve the selector through definition caches and invalidate evaluated results on version-only refreshes. Bind Client-managed Redis results to their definitions snapshot so invalidated entries cannot revive after a worker restart. Older entries without snapshot metadata become cache misses for these clients.
