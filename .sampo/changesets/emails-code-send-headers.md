---
pypi/posthog: minor
---

`emails.send`: a code send takes `headers`, such as `List-Unsubscribe` and `List-Unsubscribe-Post` for one-click unsubscribe. The SDK sends them as given, and PostHog refuses a header it doesn't allow. The template form refuses `headers`.
