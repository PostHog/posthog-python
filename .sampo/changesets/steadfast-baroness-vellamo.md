---
pypi/posthog: patch
---

Return None for malformed, empty, or whitespace-only serialized JSON feature flag payloads instead of returning the raw string or raising a JSONDecodeError. Single and bulk getters consistently decode valid JSON payloads, preserving JSON strings (including `""`), false, and zero.
