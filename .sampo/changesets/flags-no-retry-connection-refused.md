---
pypi/posthog: patch
---

Stop retrying `/flags` requests when the connection is refused, so the flag error surfaces right away instead of after the retry backoff.
