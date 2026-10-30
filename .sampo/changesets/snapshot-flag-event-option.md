---
pypi/posthog: minor
---

Add a per-read `send_feature_flag_events` option to `FeatureFlagEvaluations.is_enabled()` and `get_flag()`. Pass `send_feature_flag_events=False` to read a flag from an `evaluate_flags()` snapshot without sending `$feature_flag_called`. The read leaves the dedupe cache alone, so a later read that sends events still reports the flag, and the key still counts as accessed for `only_accessed()`. The option defaults to `True`, as on `get_feature_flag()`.
