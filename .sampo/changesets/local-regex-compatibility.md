---
pypi/posthog: patch
---

Defer feature flag regex patterns with unsupported syntax or incompatible matching semantics to server evaluation instead of returning incorrect local results. Preserve portable Unicode matching and make end-of-input anchors strict, matching the flags service.
