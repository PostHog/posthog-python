---
pypi/posthog: patch
---

Capture retries `404` and `429` responses like `408` and `500`, `502`, `503`, `504`, with the same backoff and `Retry-After` handling.
