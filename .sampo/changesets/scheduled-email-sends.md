---
pypi/posthog: minor
---

Schedule a transactional email with `scheduled_at` on `emails.send()`, up to 30 days ahead, and move, cancel or list scheduled emails with `emails.update()`, `emails.cancel()` and `emails.list()`. A naive `datetime` raises `ValueError` before any request. Send results now carry `status` and `scheduled_at`.
