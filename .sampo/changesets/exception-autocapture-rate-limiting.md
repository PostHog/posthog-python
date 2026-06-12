---
pypi/posthog: minor
---

Add client-side rate limiting to exception autocapture, matching the posthog-js and posthog-node SDKs: a token bucket per exception type allows a burst of captures, then refills over time. Rate-limited exceptions are skipped before they reach the ingestion queue. Configurable via the new `exception_autocapture_bucket_size` (default 10), `exception_autocapture_refill_rate` (default 1), and `exception_autocapture_refill_interval_seconds` (default 10) client options.
