---
pypi/posthog: minor
---

Send the same transactional email to many recipients with `emails.batch()`, one email each, from a template with `template_data` per recipient or from content in your code. The client sends at most 100 recipients and 4 MiB per request, one request after another, gives each recipient an idempotency key that every retry reuses, and returns a result per recipient: `queued`, `failed`, `unconfirmed` or `not_attempted`. A request that fails as a whole raises `EmailBatchError` with every recipient's result.
