---
pypi/posthog: minor
---

Send transactional email from content in your code with `posthog.emails.send(template_key=..., subject=..., html=...)`. The client sends the content as is, so render a React email to HTML in your app first. A send that mixes `template_id` and `template_key` fails type checks and raises `TypeError`.

A send with `template_key` captures the email's opens and clicks unless it passes `capture_engagement=False`. A template made in PostHog follows the project's setting, so a `template_id` send doesn't take `capture_engagement`. A code send takes `from_`, `reply_to` and `max_retries`. Set `migrated_from`, such as `"resend"`, on the client or on `Emails` to send it with every email request. An optional field of a code send passed as `None` counts as left out.
