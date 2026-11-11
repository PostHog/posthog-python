---
pypi/posthog: patch
---

`posthog.mcp`: on mcp 1.x (1.10 and later), a tool registered with the low-level `Server`'s `@server.call_tool()` decorator that raises now reports the exception it raised, with its cause chain, stack frames and `$mcp_error_status`, and `$mcp_error_type` names its class. Before, the decorator converted the exception into an error result first, so the failure was a generic `Error` with no frames and no status.
