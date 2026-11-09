---
pypi/posthog: patch
---

Local evaluation of relative date filters now clamps the day after every month or year subtracted, as the SDK spec requires. A `2m` filter evaluated on March 31 resolved to January 31 and now resolves to January 28, and `4y` on February 29 resolves to February 28 of the target year.
