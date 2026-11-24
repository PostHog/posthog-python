---
pypi/posthog: minor
---

Send the same transactional email to up to 100 recipients in one request with `emails.batch()`, one email each, from a template with `template_data` per recipient or from content in your code. More than 100 recipients raises `ValueError` before anything is sent. The client gives each recipient an idempotency key that every retry reuses, and returns a result per recipient: `queued`, `failed` or `unconfirmed`. A request that fails as a whole, such as a `413` `content_too_large`, raises `EmailBatchError` with every recipient's result.
