---
pypi/posthog: minor
---

Schedule a transactional email with `scheduled_at` on `emails.send()`, up to 7 days ahead, on PostHog Cloud. A naive `datetime`, a string without an offset, or a client whose host isn't PostHog Cloud (`scheduling_requires_cloud`) raises before any request. The client rounds the time up to the millisecond and checks that PostHog confirmed it, raising `schedule_not_confirmed` with the email's `message_id` otherwise. Send results now carry `status` and `scheduled_at`.
