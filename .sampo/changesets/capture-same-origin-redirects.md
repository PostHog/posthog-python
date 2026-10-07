---
pypi/posthog: major
---

Capture follows a `307` or `308` redirect only to the origin of `host`, at most 5 times, for the sync and async clients. Any other redirect fails the batch and reaches `on_error`. In 7.x the sync client followed redirects to any origin and resent the event batch there.
