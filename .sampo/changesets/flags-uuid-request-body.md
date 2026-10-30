---
posthog: patch
---

Serialize UUID values in feature flag request bodies instead of failing the evaluation, so a UUID distinct id, group key, or flag property reaches the flags service.
