---
pypi/posthog: minor
---

Send transactional email from content in your code with `posthog.emails.send(template_key=..., subject=..., html=...)`. Without `template_data`, the content is sent as is. With `template_data`, the content is a template: PostHog fills in its variables, and the client uploads it by key whenever PostHog doesn't hold its content yet, then sends again with the same idempotency key. A send that mixes `template_id` and `template_key` fails type checks and raises `TypeError`.
