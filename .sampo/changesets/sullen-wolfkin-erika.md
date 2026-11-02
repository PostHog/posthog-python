---
pypi/posthog: patch
---

Fix a race condition where concurrent feature flag fallback-cache eviction could raise KeyError instead of returning a cache miss, including during request failures.
