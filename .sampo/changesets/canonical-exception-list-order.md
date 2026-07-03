---
"posthog": minor
---

fix(errors): emit `$exception_list` in canonical order — index `0` is the caught/outermost exception, causes follow in unwrap order, and the root cause is last (previously the list was reversed with the root cause first). This aligns posthog-python with the cross-SDK exception ordering spec. Frame order within each stacktrace is unchanged.
