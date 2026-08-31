---
pypi/posthog: patch
---

Omit `$ai_input_tokens` and `$ai_output_tokens` when the provider never reported usage, instead of sending `0`, so an interrupted stream no longer looks like a free call. A zero reported by the provider is still sent, and zero keeps meaning a real report of nothing. Covers the OpenAI, Anthropic, Gemini, LangChain, OpenAI Agents and Claude Agent SDK integrations.
