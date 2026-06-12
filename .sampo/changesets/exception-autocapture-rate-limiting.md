---
pypi/posthog: minor
---

Add client-side rate limiting to exception autocapture, using the same token bucket algorithm as the posthog-js and posthog-node SDKs: a bucket per exception type allows a burst of captures, then refills over time. Rate-limited exceptions are skipped before they reach the ingestion queue. Defaults are tuned for server processes (burst of 50 per exception type, refilling 10 every 10 seconds) and configurable via the new `exception_autocapture_bucket_size`, `exception_autocapture_refill_rate`, and `exception_autocapture_refill_interval_seconds` client options.
