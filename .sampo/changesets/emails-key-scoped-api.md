---
pypi/posthog: patch
---

`posthog.emails` sends emails, uploads attachments and reads email statuses through `/api/messaging/...`, which takes the project from the project secret API key. It no longer calls `/api/projects/@current/messaging/...`, since PostHog is removing the `@current` alias. Needs PostHog Cloud, or a self-hosted PostHog from 2026-11-18 or later. Older self-hosted versions answer these calls with a 404.
