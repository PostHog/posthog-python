---
posthog: patch
---

Send tool results on AI tool spans. Claude Agent SDK tool spans now wait for the tool's result and carry `$ai_output_state`, `$ai_is_error`, `$ai_error` and `$ai_latency`. LangChain tool spans set `$ai_span_type: "tool"` and mark tools that return an error `ToolMessage`, and converted `ToolMessage`s keep `tool_call_id`, `name` and an error `status`.
