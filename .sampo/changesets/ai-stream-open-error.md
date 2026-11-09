---
pypi/posthog: patch
---

OpenAI (chat completions and responses) and Anthropic wrappers now capture an error `$ai_generation` event when a `stream=True` request fails before the stream opens (for example a 401, 429 or 5xx). Before, only mid-stream errors were captured.
