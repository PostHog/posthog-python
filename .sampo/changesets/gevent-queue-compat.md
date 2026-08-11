---
pypi/posthog: patch
---

fix: deliver events under gevent monkey-patching by giving lanes an SDK-owned queue with CPython `queue.Queue` semantics; previously gevent's replacement `queue.Queue` lacked the private synchronization attributes the consumer and `flush()` rely on, so gevent gunicorn workers silently dropped every event.
