---
pypi/posthog: patch
---

fix: `flush()` no longer waits out `flush_interval` before delivering a partial batch. A consumer holding fewer than `flush_at` events now sends them as soon as `flush()` (or `shutdown()`) asks it to, instead of blocking the caller for the rest of the batching window — which previously made `flush()` deliver nothing at all when `flush_interval` was longer than the flush timeout. Timer-based batching without an explicit flush is unchanged.
