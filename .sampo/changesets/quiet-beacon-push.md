---
pypi/posthog: minor
---

Add `posthog.push.send()`, `AsyncPosthog.push.send()` and the standalone `Push` and `AsyncPush` classes to send transactional push notifications from a PostHog push template to every device of one person. Sends use the client's `secret_key` (a project secret API key with the `transactional_push:write` scope), `app_host`, and email request timeout and retries.
