---
pypi/posthog: patch
---

Warn when an AI wrapper's `base_url` points at the PostHog AI Gateway. The gateway emits its own `$ai_generation`, so each call would be captured (and billed) twice. The wrapper only warns and never drops the event. Detection covers the wrapper funnels (OpenAI, Anthropic, LangChain) and the OTel span path.
