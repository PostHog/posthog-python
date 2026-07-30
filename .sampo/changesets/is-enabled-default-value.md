---
pypi/posthog: minor
---

feat: `FeatureFlagEvaluations.is_enabled()` accepts a `default_value` returned when the flag has no value in the evaluation — the key was not part of the evaluated set, or the evaluation came back empty (failed `/flags` request, quota limit, no resolvable `distinct_id`). A flag that has a value still wins, so a disabled flag returns `False` even with `default_value=True`. The default is `False`, so existing calls behave exactly as before.
