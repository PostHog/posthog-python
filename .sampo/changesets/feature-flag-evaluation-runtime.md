---
pypi/posthog: minor
---

Read a feature flag's evaluation runtime from the SDK: `get_feature_flag_evaluation_runtime(key)` returns the `FeatureFlagEvaluationRuntime` on a locally loaded flag definition, and `get_feature_flag_keys_by_evaluation_runtime(runtime)` lists the keys a given runtime can evaluate. The local-evaluation payload already carried the value, but only on the untyped `client.feature_flags` dicts, so a backend that serves flags to its own frontend had to call `/api/feature_flag/local_evaluation` itself to see it.
