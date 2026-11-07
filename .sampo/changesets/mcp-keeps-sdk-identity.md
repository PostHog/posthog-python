---
pypi/posthog: major
---

MCP instrumentation no longer relabels the whole client. In 7.x, `posthog.mcp.instrument()` and `PostHogMCP` changed `$lib`, the `PostHog-Sdk-Info` header and the feature flag request `User-Agent` for everything the client sent, including the app's own events, and `instrument()` without a client relabeled the global client. Now only MCP analytics events report `posthog-python-mcp`, and everything else reports `posthog-python`.
