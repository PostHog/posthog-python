---
pypi/posthog: patch
---

Streaming Gemini calls that fail now record the HTTP status of the google-genai error (for example 429 or 503) in `$ai_http_status`. Before, `capture_streaming_event` read only `status_code`, so these events were captured with a status of 0.
