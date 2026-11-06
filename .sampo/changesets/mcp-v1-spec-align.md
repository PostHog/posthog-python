---
pypi/posthog: minor
---

MCP analytics now matches the cross-SDK MCP spec ahead of 1.0:

- `instrument()` sends `$mcp_unknown_tool` instead of a failed `$mcp_tool_call` and `$exception` when a `tools/call` names a tool your server never registered. Insights that count failed tool calls stop counting these. A call naming no tool sends nothing. A raw low-level server has no registry the SDK can read, so a failed call there stays a failed tool call.
- FastMCP's stock logging, timing, error-handling, and similar middleware no longer turns off `llm_model` injection on standalone `fastmcp.FastMCP`.
- Manual capture without a `session_id` now sends a fresh `ses_` session id and uses it as the `distinct_id`, instead of no `$session_id` and an `anonymous` person.
- Truncated string properties now fit their limit with the `...` marker included, so `$mcp_tool_name` and other names stop at 256 characters instead of 259.
