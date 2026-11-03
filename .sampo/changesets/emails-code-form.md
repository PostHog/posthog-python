---
pypi/posthog: minor
---

Send transactional email from content in your code with `posthog.emails.send(template_key=..., subject=..., html=...)`. The client sends the content as is, so render a React email to HTML in your app first. A send that mixes `template_id` and `template_key` fails type checks and raises `TypeError`.

A send with `template_key` captures the email's opens and clicks unless it passes `capture_engagement=False`. A template made in PostHog follows the project's setting, so a `template_id` send doesn't take `capture_engagement`. A code send takes `from_`, `reply_to`, `cc`, `bcc`, `attachments` and `max_retries`. Set `migrated_from`, such as `"resend"`, on the client or on `Emails` to send it with every email request. An optional field of a code send passed as `None` counts as left out.

An attachment is a `filename` and its `content`, with an optional `content_type` and `content_id`. Its `content` can be `bytes`, `bytearray` or `memoryview`, which the client encodes as Base64 once per send, or a Base64 string, which it sends as is. PostHog can't fetch an attachment from a path or URL. An attachment the client can't send raises `EmailError` with the code `invalid_attachment` before any request, and the error never repeats the file's content. A `template_id` send doesn't take `cc`, `bcc` or `attachments`.
