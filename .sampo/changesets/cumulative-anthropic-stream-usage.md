---
pypi/posthog: patch
---

Streamed Anthropic generations now take the input and cache token counts from the final `message_delta` usage, which is cumulative. With server tools such as web search, those counts grow past the `message_start` values, so streamed calls under-reported input tokens and cache writes.
