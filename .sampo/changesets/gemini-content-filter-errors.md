---
pypi/posthog: patch
---

Capture Gemini generations that the content filters blocked (a blocked prompt, or a response stopped with a filter finish reason such as `PROHIBITED_CONTENT`) with `$ai_is_error` and an `$ai_error` that names the reason.
