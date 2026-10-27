---
pypi/posthog: patch
---

Capture makes one connection try per attempt, so every physical request carries its own attempt number and request timestamp. 7.x also retried a failed connection up to twice inside each attempt, reusing the request timestamp, which shifted the events' stored time by the time spent connecting.

When `before_send` raises, the error line names the event and the exception type, not the exception message. The traceback is logged at debug level.

Events without `NaN` or `Infinity` are encoded once, without first copying them to replace non-finite numbers. Events that contain them still send those values as `null`.
