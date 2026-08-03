---
pypi/posthog: patch
---

Local evaluation now clears the stored flag-definition ETag whenever it drops its definitions after a quota-limited (402) or unauthorized (401) response. Previously the ETag survived the reset, so the next poll asked the server conditionally for definitions the SDK no longer held, got a `304 Not Modified`, and left local evaluation with an empty definition set until the flag definitions happened to change server-side.
