---
pypi/posthog: patch
---

`posthog.emails` sends emails, uploads attachments and reads email statuses through `/api/messaging/...`, which takes the project from the project secret API key. It no longer calls `/api/projects/@current/messaging/...`, since PostHog is removing the `@current` alias. A self-hosted PostHog older than these routes answers these calls with 404.
