---
pypi/posthog: patch
---

Local evaluation now treats an empty-string, `None`, boolean, list or dict group key as no group key, as the flags service does. A flag aggregated on that group type resolves as `false` locally without a `/flags` request, a mixed flag skips its conditions on that group type, and a group dependency with such a key resolves as `false`. Before, an empty string was used as the bucketing key and the other kinds fell back to `/flags`.
