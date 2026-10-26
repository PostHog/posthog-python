---
pypi/posthog: minor
---

Send transactional email from PostHog email templates with `posthog.emails.send()`, or with `Emails` and `AsyncEmails` from `posthog.emails` in a process that only sends email. A send needs a project secret API key, goes to the app host, sends an idempotency key, and retries network errors, rate limits and an unavailable service with the same key. Errors raise `EmailError` with PostHog's error `code` and a Resend-style `name`.
