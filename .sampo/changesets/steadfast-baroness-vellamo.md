---
pypi/posthog: patch
---

Return None for malformed JSON feature flag payloads instead of returning the raw string or raising a JSONDecodeError.
