---
pypi/posthog: patch
---

`$mcp_tools_list` events no longer copy the tool descriptors into `$mcp_response`, which keeps only the response envelope such as `nextCursor`. The tool names stay in `$mcp_listed_tool_names`.
