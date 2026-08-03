---
pypi/posthog: minor
---

Capture Gemini thought summaries as `thinking` content blocks. When a request enables `thinking_config.include_thoughts`, parts marked `thought=True` in responses, inputs, and streaming chunks are now formatted as `{"type": "thinking", "thinking": ...}` (matching the Anthropic thinking-block shape PostHog renders as reasoning) instead of plain text blocks.
