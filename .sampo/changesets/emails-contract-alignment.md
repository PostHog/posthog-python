---
pypi/posthog: minor
---

`emails.send` follows the shared email SDK contract. An argument that fails a check raises `EmailValidationError`, an `EmailError` and a `ValueError` with the check's `code` and the argument in `field`. A disabled client raises `client_disabled`, and a client without a usable app host raises `missing_app_host`. A send takes `max_retries`, and a template send takes `capture_engagement`. Set `migrated_from`, such as `"resend"`, on the client to send it with every email request. A `Retry-After` over 30 seconds waits 30 seconds instead of failing the send. `SendEmailResult` has no `replayed` field.
