---
pypi/posthog: patch
---

`capture_exception()` called without an exception now marks the exception it takes from `sys.exc_info()` as captured, so a later automatic capture of the same exception no longer sends a second `$exception` event.
