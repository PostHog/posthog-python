---
pypi/posthog: patch
---

Return None for malformed, empty, or whitespace-only serialized JSON feature flag payloads instead of returning the raw string or raising a JSONDecodeError. Legacy bulk getters (`get_all_flags_and_payloads`, `get_feature_flags_and_payloads`, and `get_feature_payloads`) validate payloads but preserve valid serialized JSON for compatibility with callers using `json.loads()`. Single-flag and `evaluate_flags()` snapshot payload getters continue returning decoded values, including JSON strings (such as `""`), false, and zero. Reject non-JSON constants such as NaN and Infinity and isolate decoder-limit failures so flag values and healthy sibling payloads remain available.
