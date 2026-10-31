---
pypi/posthog: patch
---

`PostHogMCP.prepare_tool_list` now adds the `context` argument to MCP SDK 2.x `Tool` objects. It only added it to dicts and 1.x `Tool` objects before, so `$mcp_intent` was never captured for 2.x tool objects.
