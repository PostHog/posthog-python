---
pypi/posthog: patch
---

MCP tool failures now report the exception the tool actually raised on `$mcp_error_message` and `$mcp_error_type`, stepping past the SDK's dispatch `ToolError` wrapper. mcp 2.1 masks the original message out of that wrapper, which left the failures view with only `Error executing tool <name>`. The `$exception` sibling still carries the full chain.
