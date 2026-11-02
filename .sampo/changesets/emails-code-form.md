---
pypi/posthog: minor
---

Send transactional email from content in your code with `posthog.emails.send(template_key=..., subject=..., html=...)`. Without `template_data`, the content is sent as is. With `template_data`, the content is a template: PostHog fills in its variables, and the client uploads it by key whenever PostHog doesn't hold its content yet, then sends again with the same idempotency key. A local template needs `html`. A send that mixes `template_id` and `template_key` fails type checks and raises `TypeError`.

A send with `template_key` captures the email's opens and clicks unless it passes `capture_engagement=False`. A template made in PostHog follows the project's setting, so a `template_id` send doesn't take `capture_engagement`. A code send takes `max_retries` like a template send. Set `migrated_from`, such as `"resend"`, on the client or on `Emails` to send it with every email request.

A code send also takes `cc`, `bcc`, `headers`, `tags` and `attachments`. An attachment's `content` can be `bytes`, `bytearray` or `memoryview`, which the client encodes as Base64, or a Base64 string. An optional field of a code send passed as `None` counts as left out.
