---
pypi/posthog: patch
---

`posthog.ai.langchain`: stop double-counting Gemini thinking tokens. langchain-google-genai reports `usage_metadata.output_tokens` with reasoning included (it adds `thoughts_token_count`), and PostHog prices Gemini 2.5/3 reasoning on top of `$ai_output_tokens`, so thinking was billed twice. For Gemini models whose output includes reasoning (input plus output reach `total_tokens`), the handler now reports `$ai_output_tokens` without it, as the Gemini wrapper does. Integrations that already report candidates only, such as ChatVertexAI, are unchanged.
