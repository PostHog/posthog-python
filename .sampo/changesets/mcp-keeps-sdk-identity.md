---
pypi/posthog: major
---

MCP instrumentation no longer relabels the whole client. In posthog-python 7.x.x, `posthog.mcp.instrument()` and `PostHogMCP` changed `$lib` on every event the client sent, including the app's own, and the SDK name in its request headers, feature flag requests included. `instrument()` without a client relabeled the global client. Now only MCP analytics events report `posthog-python-mcp`, and everything else reports `posthog-python`. The label is set in each MCP event's own properties, so a `$lib` in `super_properties` no longer replaces it. `before_send` can.
