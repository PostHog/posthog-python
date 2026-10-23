---
pypi/posthog: patch
---

Load feature flag definitions from a `flag_definition_cache_provider` when no `secret_key` is set, so local evaluation works with a cache provider alone.
