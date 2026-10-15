---
pypi/posthog: patch
---

`Prompts.get()` now makes one request when several threads ask for the same prompt at once, instead of one request per thread. The other threads wait for that result. With `capture_errors=True`, a shared failure is reported once instead of once per thread.
