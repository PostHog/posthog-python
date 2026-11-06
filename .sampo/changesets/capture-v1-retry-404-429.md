---
pypi/posthog: patch
---

With `capture_mode="v1"`, capture now retries `404` and `429` responses like `408` and `500`, `502`, `503`, `504`, with the same backoff and `Retry-After` handling. Before, both failed the batch at once.
