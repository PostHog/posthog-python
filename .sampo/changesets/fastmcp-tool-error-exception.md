---
pypi/posthog: patch
---

`posthog.mcp`: a failed tool call on a standalone FastMCP server now reports the exception the tool raised, with its cause chain and stack frames, and `$mcp_error_type` names its class. Before, FastMCP converted the exception into an error result first, so the failure was a generic `Error` with no frames.
