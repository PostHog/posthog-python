---
pypi/posthog: minor
---

MCP events take capture options. The `PostHogMCP` capture methods and `McpAnalytics.capture` take an `event_options` argument, and `MCPAnalyticsOptions(event_options=...)` sets options on every event `instrument()` captures automatically, from a function of `(request, extra)` like `event_properties`. An event without an identity still always sends `process_person_profile: False`. `PostHogMCP.capture()` keeps the core client's `options` argument.
