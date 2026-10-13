---
pypi/posthog: patch
---

Keep `$release_id` on minimized `$feature_flag_called` events. The property is a static, low-cardinality runtime identity dimension, so the capture contract requires the minimal-event allowlist to retain it when the SDK stamped it from `POSTHOG_RELEASE_ID`.
