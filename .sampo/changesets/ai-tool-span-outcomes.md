---
pypi/posthog: patch
---

`posthog.ai.claude_agent_sdk` and `posthog.ai.langchain`: tool spans now carry the tool's outcome, so per-tool failure rates can be computed. The Claude Agent SDK integration emits each tool `$ai_span` when its `ToolResultBlock` arrives, with `$ai_output_state`, `$ai_is_error`, `$ai_error` and `$ai_latency` (a tool use whose result never arrives is still emitted when the query or session ends). The LangChain callback handler sets `$ai_span_type: "tool"` on tool spans, marks a tool error handled by `handle_tool_error` (a `ToolMessage` with `status="error"`) with `$ai_is_error` and `$ai_error`, and keeps `tool_call_id`, `name` and an error `status` on tool messages in `$ai_input`.
