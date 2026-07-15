---
pypi/posthog: minor
---

Every `$feature_flag_called` event now carries a `$feature_flag_has_experiment` boolean property reflecting the server-reported `has_experiment` signal for the flag. When the server does not report the field (older deployments), it defaults to `false`.
