---
pypi/posthog: minor
---

Add opt-in client-side rate limiting for exception autocapture, using the same token bucket algorithm as the posthog-js and posthog-node SDKs: a bucket per exception type allows a burst of captures, then refills over time. Rate-limited exceptions are skipped before they reach the ingestion queue. Disabled by default; enable with the new `enable_exception_autocapture_rate_limiting` client option and tune via `exception_autocapture_bucket_size` (default 50), `exception_autocapture_refill_rate` (default 10), and `exception_autocapture_refill_interval_seconds` (default 10).
