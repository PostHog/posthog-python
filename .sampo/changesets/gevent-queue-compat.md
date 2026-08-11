---
pypi/posthog: patch
---

fix: deliver events under gevent monkey-patching by giving lanes an SDK-owned queue (`LaneQueue`) with CPython `queue.Queue` semantics, including Python 3.13's `shutdown()`/`ShutDown` API; previously gevent's replacement `queue.Queue` lacked the private synchronization attributes the consumer and `flush()` rely on, so gevent gunicorn workers silently dropped every event. Note for integrators reaching into the backwards-compatible `Client.queue` property: the concrete type is now `posthog._queue.LaneQueue`, which matches `queue.Queue`'s full attribute surface but is deliberately not an instance of `queue.Queue` (inheriting would re-import the gevent bug).
