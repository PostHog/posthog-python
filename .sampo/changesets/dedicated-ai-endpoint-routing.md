---
pypi/posthog: patch
---

Add internal-only routing of `$ai_*` events to a dedicated capture endpoint in their own batch, gated behind the unstable `_dedicated_ai_endpoint` client option (off by default, not for general use).
