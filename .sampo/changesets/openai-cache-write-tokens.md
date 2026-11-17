---
pypi/posthog: patch
---

`posthog.ai.openai`: report OpenAI cache-write tokens as `$ai_cache_creation_input_tokens`, for streaming and non-streaming calls. OpenAI reports them in `input_tokens_details.cache_write_tokens` (Responses) or `prompt_tokens_details.cache_write_tokens` (Chat Completions) and bills them above the input rate, but the wrapper read only `cached_tokens`, so cache writes were costed as plain input tokens.
