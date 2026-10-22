---
pypi/posthog: patch
---

Streamed OpenAI, Anthropic and Gemini generations that raise mid-stream are now captured with `$ai_is_error` and `$ai_error` (and the error's status code as `$ai_http_status`) instead of being recorded as a successful 200. The error is still re-raised to the caller, matching what non-streaming calls already did.
