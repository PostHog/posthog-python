---
pypi/posthog: patch
---

The LangChain callback handler no longer reports too few input tokens for a non-streamed `ChatAnthropic` call that reads or writes the prompt cache. It took the raw Anthropic usage from `response_metadata`, whose input tokens already exclude the cache, and subtracted the cache tokens again, so a cached call usually reported 0 input tokens. It now reads LangChain's standard `usage_metadata` first, as it already did for streamed calls.
