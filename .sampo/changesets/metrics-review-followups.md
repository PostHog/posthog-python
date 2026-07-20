---
pypi/posthog: patch
---

Harden the alpha `posthog.metrics` client based on review follow-ups.

- Metric attributes are now deep-snapshotted at capture time, so mutating a nested list/dict value after `count()`/`gauge()`/`histogram()` can no longer rewrite an already-recorded series' attributes on the wire.
- Failed metric flushes now retry with exponential backoff (first retry at the base interval, then doubling per consecutive failure, capped at 64x the flush interval — the shared JS logs ramp) instead of the fixed cadence, and the buffered window is dropped loudly after 8 consecutive failed flushes — previously documented as 3 but effectively 4.
- Invalid `metrics` client config (non-dict config or `resource_attributes`, non-numeric `flush_interval`, non-integer `max_series_per_flush`, non-callable `before_send`) now degrades to defaults with a warning instead of raising from the first `client.metrics.count()` call, matching the client's no-throw contract.
