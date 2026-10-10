---
pypi/posthog: major
---

The AI integrations no longer fall back to `capture` for a client object without `capture_ai`. Creating an OpenAI, Anthropic or Gemini wrapper, the LangChain `CallbackHandler`, or the OpenAI Agents or Claude Agent SDK processor with such a client now raises `TypeError`. The error shows up when you set the integration up, and not after a provider call. A `Client` or `AsyncPosthog` instance, and the default global client, work as before.
