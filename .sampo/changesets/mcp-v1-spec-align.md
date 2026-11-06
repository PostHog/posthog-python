---
pypi/posthog: minor
---

MCP analytics now matches the cross-SDK MCP spec ahead of 1.0:

- A `tools/call` naming a tool your server never registered sends `$mcp_unknown_tool` instead of a failed `$mcp_tool_call` and `$exception`. Custom dispatchers can send it with `PostHogMCP.capture_unknown_tool()`. Insights that count failed tool calls stop counting these. A call naming no tool sends nothing.
- A tool your server's `tools/list` handler does not list, such as a hidden or per-user tool, now counts as an unknown tool when a call to it fails.
- On a low-level server whose tool registry the SDK can't read, a failed call runs your `tools/list` handler once to check the name, waiting at most one second. A handler that is slower leaves the call recorded as a failed tool call.
- Manual capture without a `session_id` now sends a fresh `ses_` session id and uses it as the `distinct_id`, instead of no `$session_id` and an `anonymous` person.
- Truncated string properties now fit their limit with the `...` marker included, so `$mcp_tool_name` and other names stop at 256 characters instead of 259.
- `get_more_tools` and `send_feedback` declare `readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint` as `false`, and their annotations no longer carry a `title`.
- The injected `context` argument has a shorter description, the same in every PostHog MCP SDK.
- The injected `llm_model` argument is always optional.

The annotations and descriptions change your published tool definitions. If your server is listed in the ChatGPT or Claude directories, those platforms may re-review the updated tools.
