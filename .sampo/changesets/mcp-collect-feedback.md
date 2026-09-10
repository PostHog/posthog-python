---
pypi/posthog: minor
---

Add an opt-in `collect_feedback` option to MCP analytics. It injects a `send_feedback` virtual tool and captures every call as a `$mcp_feedback` event, so agents can report a missing capability, a tool problem, or praise.
The option supports a custom tool name and description, host-declared extra schema fields, and an `on_feedback` handler that routes reports to a real backend. `PostHogMCP` gains the same option plus `capture_feedback` for custom dispatchers.
