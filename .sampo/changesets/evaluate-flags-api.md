---
pypi/posthog: minor
---

Add `evaluate_flags()` and a new `flags` option on `capture()` so a single `/flags` call can power both flag branching and event enrichment per request:

```python
flags = posthog.evaluate_flags(distinct_id, person_properties={"plan": "enterprise"})
if flags.is_enabled("new-dashboard"):
    render_new_dashboard()
posthog.capture("page_viewed", distinct_id=distinct_id, flags=flags)
```

The returned `FeatureFlagEvaluations` snapshot exposes `is_enabled()`, `get_flag()`, `get_flag_payload()` for branching and `only_accessed()` / `only([keys])` filter helpers. Pass `flag_keys=[...]` to `evaluate_flags()` to scope the underlying `/flags` request itself.

Existing `feature_enabled()`, `get_feature_flag()`, `get_feature_flag_payload()`, and `capture(send_feature_flags=...)` continue to work unchanged. They will be deprecated in a follow-up minor and removed in the next major version.
