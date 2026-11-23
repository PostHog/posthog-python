---
pypi/posthog: patch
---

`posthog.ai.openai_agents`: report token counts on response spans when the Agents SDK runs with `trace_include_sensitive_data=False`. The SDK then leaves `span_data.response` unset but still records usage on `span_data.usage`, which the processor ignored, so those `$ai_generation` events had no input, output or cache-read tokens.
