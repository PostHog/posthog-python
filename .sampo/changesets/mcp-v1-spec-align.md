---
pypi/posthog: minor
---

MCP analytics now matches the cross-SDK MCP spec ahead of 1.0:

- A `tools/call` naming a tool your server never registered sends `$mcp_unknown_tool` instead of a failed `$mcp_tool_call` and `$exception`. Custom dispatchers can send it with `PostHogMCP.capture_unknown_tool()`. Insights that count failed tool calls stop counting these.
- `get_more_tools` and `send_feedback` declare `readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint` as `false`, and their annotations no longer carry a `title`.
- The injected `context` argument has a shorter description, the same in every PostHog MCP SDK.
- The injected `llm_model` argument is always optional.

The annotations and descriptions change your published tool definitions. If your server is listed in the ChatGPT or Claude directories, those platforms may re-review the updated tools.
