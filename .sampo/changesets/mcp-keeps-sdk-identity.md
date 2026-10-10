---
pypi/posthog: major
---

MCP instrumentation no longer relabels the client as `posthog-python-mcp`. `posthog.mcp.instrument()` and `PostHogMCP` used to change `$lib`, the `PostHog-Sdk-Info` header and the feature flag request `User-Agent` for every event the client sent, including the app's own events. All events now report `posthog-python`. Filter MCP traffic on the `$mcp_*` events and properties.
