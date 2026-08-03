---
pypi/posthog: minor
---

`posthog.ai.openai.OpenAI` / `AsyncOpenAI` now accept a per-call `posthog_provider_override` argument. The wrapper is commonly pointed at OpenAI-compatible endpoints (DeepSeek, Groq, Mistral, Together, Fireworks, xAI, Perplexity, Ollama, Cerebras, and various gateways) via a custom `base_url`, but always reported `$ai_provider: "openai"`, which breaks PostHog's cost attribution for those calls. Passing `posthog_provider_override="deepseek"` (for example) sets `$ai_provider` on the emitted event without changing how the OpenAI-shaped response is parsed. Omitting it leaves `$ai_provider` as `"openai"`, exactly as before. Covers chat completions, the Responses API, `.parse()`, and embeddings, across sync, async, and streaming calls.
