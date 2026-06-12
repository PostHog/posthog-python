---
pypi/posthog: minor
---

Add client-side rate limiting to exception autocapture, matching the posthog-js and posthog-node SDKs: a token bucket per exception type allows a burst of captures (bucket size 10), then refills one capture every 10 seconds. Rate-limited exceptions are skipped before they reach the ingestion queue.
