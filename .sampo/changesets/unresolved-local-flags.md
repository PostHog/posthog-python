---
pypi/posthog: minor
---

Report flags that local evaluation could not resolve. `FeatureFlagEvaluations.unresolved_flags` maps each such flag to an `UnresolvedFlagReason`, and reading one reports the `local_evaluation_inconclusive` error instead of `flag_missing`. Log a warning once per definition when a loaded flag has experience continuity enabled, and evaluate an inactive flag with experience continuity to `false` locally.
