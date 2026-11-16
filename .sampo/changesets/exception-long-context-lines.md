---
pypi/posthog: patch
---

Exception autocapture now sends a truncated string for source lines over 1024 bytes in a frame's `context_line`, `pre_context` and `post_context`. Before, those lines were sent as `null`.
