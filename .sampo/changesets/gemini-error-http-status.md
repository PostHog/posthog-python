---
pypi/posthog: patch
---

Failed Gemini calls are now captured with the HTTP status of the error as `$ai_http_status` (for example 429 or 503) instead of 0. google-genai errors carry the status as `code` rather than `status_code`, which the OpenAI and Anthropic wrappers already read.
