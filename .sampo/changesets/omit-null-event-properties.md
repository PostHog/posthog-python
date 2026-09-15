---
pypi/posthog: patch
---

Omit null-valued custom object properties recursively when serializing events, while preserving null array elements, caller inputs, and typed exception and feature-flag metadata.
