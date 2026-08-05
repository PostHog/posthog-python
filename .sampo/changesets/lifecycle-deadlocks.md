---
pypi/posthog: patch
---

fix: prevent client lifecycle deadlocks when error callbacks, concurrent `join()`/`shutdown()` calls, or forked sync-mode clients interact with queue and worker teardown.
