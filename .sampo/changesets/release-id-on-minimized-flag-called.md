---
posthog: patch
---

A minimized `$feature_flag_called` event keeps `$release_id`. The release id the SDK reads once from `POSTHOG_RELEASE_ID` was stamped on the event and then stripped by the minimal-event allowlist, so flag calls could not be broken down by release. It is static, low-cardinality runtime identity, like the `$lib` and `$os` values the allowlist already keeps.
