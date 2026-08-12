---
pypi/posthog: patch
---

fix: preserve event delivery when gevent monkey-patches `queue.Queue`, including in preloaded gunicorn workers
