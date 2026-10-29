---
pypi/posthog: patch
---

Capture an `$ai_generation` error event when `create(stream=True)` fails before the stream starts (bad key, rate limit, invalid model) in the OpenAI chat, OpenAI Responses, Anthropic and Gemini wrappers, sync and async. Previously only errors raised mid-stream were captured.
