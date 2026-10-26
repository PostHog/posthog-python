---
pypi/posthog: minor
---

Send transactional email from PostHog email templates with `client.emails.send()` on `Posthog` and `AsyncPosthog`, or with `Emails` and `AsyncEmails` from `posthog.emails` in a process that only sends email. A send needs a project secret API key, goes to the app host, sends an idempotency key, and retries network errors, rate limits and an unavailable service with the same key. Set `app_host`, `email_request_timeout_seconds` and `email_request_max_retries` on the client to configure sends. Errors raise `EmailError` with PostHog's error `code` and a Resend-style `name`.
