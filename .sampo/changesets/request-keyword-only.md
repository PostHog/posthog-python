---
pypi/posthog: major
---

The parameters after `path` in `posthog.request.post` and after `host` in `posthog.request.flags` are keyword-only. With `gzip` removed, a 7.x call that passed them by position bound them to the wrong parameter. It now raises `TypeError`.
