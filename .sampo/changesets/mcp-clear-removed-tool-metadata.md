---
pypi/posthog: patch
---

`posthog.mcp`: stop reporting a stale `$mcp_tool_description` or `$mcp_tool_category` on `$mcp_tool_call` events when a later `tools/list` still lists the tool but with an empty or missing description, or without `_meta.category`. The cache kept the value from the earlier listing, so events carried text the server no longer advertised; the property is now omitted.