---
pypi/posthog: patch
---

Return None for malformed, empty, or whitespace-only serialized JSON feature flag payloads instead of returning the raw string or raising a JSONDecodeError. Single and bulk getters (including `get_feature_payloads` and `get_feature_flags_and_payloads`) consistently decode valid JSON payloads, preserving JSON strings (including `""`), false, and zero. Reject non-JSON constants such as NaN and Infinity and isolate decoder-limit failures so flag values and healthy sibling payloads remain available.
