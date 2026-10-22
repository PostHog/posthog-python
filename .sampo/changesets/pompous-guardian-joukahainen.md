---
pypi/posthog: patch
---

Fix local feature flag string and regex operators to match an explicit null property as "null", like the flags service. This can change flag values or selected variants for explicitly null person or group properties. Missing properties still allow remote evaluation fallback.
