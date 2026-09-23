---
pypi/posthog: patch
---

Honor `filters.holdout` during local feature flag evaluation. A user in an experiment holdout now receives the `holdout-<id>` variant instead of being bucketed into a regular variant, matching how the server evaluates the same flag. Holdout membership is resolved before release conditions, so a held-out user never reaches the flag's targeting.
