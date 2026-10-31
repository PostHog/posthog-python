---
pypi/posthog: patch
---

Add `$feature_flag_id` and `$feature_flag_version` to `$feature_flag_called` events for locally evaluated flags in `get_feature_flag()`, `feature_enabled()` and `get_feature_flag_result()`, and `$feature_flag_version` for locally evaluated flags in `evaluate_flags()`, taken from the flag definition. Remotely evaluated flags already sent both.
