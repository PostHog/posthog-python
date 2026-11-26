---
pypi/posthog: patch
---

Local evaluation now treats an empty-string, `None`, boolean, list or dict group key as no group key, as the flags service does. Conditions aggregated on that group type are skipped locally without a `/flags` request, so a flag with only such conditions resolves as `false`, directly or as a dependency. Before, an empty string was used as the bucketing key and the other kinds fell back to `/flags`.
