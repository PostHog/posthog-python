---
pypi/posthog: minor
---

feat(ai): emit an `$ai_span` for each tool call an LLM requests in the OpenAI, Anthropic, and Gemini wrappers. When a generation's response contains tool calls, the wrapper now captures a child span (`$ai_span_type: "tool"`) per call — carrying the tool name, arguments, and tool-call id — nested under the generation via `$ai_parent_id`, so requested tools show up in the trace tree. Generation events also now carry an `$ai_span_id`. Works for sync, async, and streaming calls; span capture is best-effort and never breaks the underlying LLM call.
