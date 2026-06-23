---
'pypi/posthog': minor
---

Add `posthog.mcp`, a Python SDK for PostHog MCP analytics (just `pip install posthog`; the MCP SDK is a peer dependency of `instrument()`, not bundled). `instrument(server, posthog_client)` wraps a `FastMCP` or low-level `mcp.server.Server` so every tool call, agent intent, tools/list, initialize, and failure is captured to PostHog as a `$mcp_*` event. Also adds `PostHogMCP`, a `Client` subclass for custom dispatchers (needs nothing beyond posthog), plus opt-in `context` intent capture, `identify`, `report_missing` (`get_more_tools`), and `conversation_id`. Beta.
