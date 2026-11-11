---
pypi/posthog: patch
---

Fix `flag_fallback_cache_url` for Redis URLs with an IPv6 host: the brackets are kept, so the Redis flag cache is no longer disabled.
