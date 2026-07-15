---
pypi/posthog: minor
---

`$feature_flag_called` events now carry a `$feature_flag_has_experiment` boolean property when the server reports whether the flag is linked to an experiment. When the server does not report the signal (older deployments), the property is omitted.
