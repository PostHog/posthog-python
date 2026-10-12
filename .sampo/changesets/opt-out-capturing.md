---
pypi/posthog: minor
---

Add `opt_out_capturing()`, `opt_in_capturing()` and `is_opted_out()` to `Client` and the module-level API so capture can be suppressed at runtime. While opted out, events are dropped silently, metrics and spans are no longer recorded or exported, and none of them make a network request; feature flag evaluation is unaffected. Opt-out is separate from `disabled`, and opting in never re-enables a disabled client. `AsyncClient` does not support opt-out yet.
