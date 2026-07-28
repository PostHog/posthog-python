---
pypi/posthog: patch
---

Stop `posthog.get_feature_flag_payload()` sending `$feature_flag_called` events by default, matching `Client.get_feature_flag_payload()`
