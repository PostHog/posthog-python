---
pypi/posthog: minor
---

Schedule a transactional email with `scheduled_at` on `emails.send()`, up to 30 days ahead, and move, cancel or list scheduled emails with `emails.update()`, `emails.cancel()` and `emails.list()`. A naive `datetime` raises `ValueError` before any request. After a move that got no answer, the client refuses to move that email to another time, with `move_unconfirmed`, since PostHog may still apply the late move. Send results now carry `status` and `scheduled_at`.
