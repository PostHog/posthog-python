---
pypi/posthog: minor
---

`Prompts.get_all()` now works without a label: it fetches the latest version of every prompt in one request and caches each one under the key `get(name)` reads. Apps that do not use labels no longer need one request per prompt per cache cycle.
