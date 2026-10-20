---
pypi/posthog: patch
---

Fix local feature flag `gt`, `gte`, `lt`, and `lte` comparisons to compare numeric strings numerically rather than lexicographically. This can change which users match flags targeting numeric-string properties. Only complete, unpadded decimal strings and finite numbers are accepted: invalid properties no longer compare lexicographically, and invalid filter values allow remote evaluation fallback.
