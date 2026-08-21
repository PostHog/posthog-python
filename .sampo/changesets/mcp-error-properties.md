---
posthog: minor
---

feat(mcp): emit `$mcp_error_message` and `$mcp_error_type` on failed MCP events. The reason a tool call failed previously lived only on the sibling `$exception` event, so PostHog's failures view — which reads the scalars off the primary event — showed empty error rows for every Python-backed MCP server, and switching off `enable_exception_autocapture` removed the reason entirely. Both values are read from the same `$exception_list` the sibling carries, so the two surfaces can never disagree, and the message inherits the existing 2048-character cap. `PostHogMCP.capture_tool_call()` and `capture_tools_list()` take a new optional `error_type` for custom dispatchers that want a coarse category (`"validation"`, `"timeout"`) instead of the thrown class name. Parity with `@posthog/mcp`.
