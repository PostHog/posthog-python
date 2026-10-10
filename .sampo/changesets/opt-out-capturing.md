---
pypi/posthog: minor
---

Add `opt_out_capturing()`, `opt_in_capturing()` and `is_opted_out()` so capture can be suppressed at runtime. While opted out, events are dropped silently with no network request; feature flag evaluation is unaffected. This is separate from the constructor-time `disabled` kill switch, which still cannot be toggled after construction.
