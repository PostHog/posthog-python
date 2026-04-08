---
pypi/posthog: patch
---

fix: Django middleware accidentally passed capture_exceptions as positional arg, setting fresh=True and resetting context state
