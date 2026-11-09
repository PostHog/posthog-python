---
pypi/posthog: patch
---

An exception passed to `capture_exception` without being raised now gets its synthesized stack in outermost-first order, without the SDK's own capture frames, and with `mechanism.synthetic` set to `true`.
