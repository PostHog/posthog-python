---
pypi/posthog: patch
---

Streamed OpenAI-compatible chat completions whose tool call deltas carry no `index` are no longer dropped from `$ai_output`. A delta with a new id starts a call, and one without an id continues the latest call.
