---
pypi/posthog: patch
---

MCP analytics now surfaces the previously-silent case where the stateless session mint middleware (`PostHogMcpStatelessSessionMiddleware`) never attached — the trap where an ASGI app is built or mounted before `instrument()` runs, so autowiring can't retrofit it and every session falls back to a fragmented per-process id. `instrument()` warns when `streamable_http_app()` was already called before it ran, and a one-time warning fires the first time a tool call arrives over streamable HTTP and the session still has to come from process memory. Both go to the `posthog.mcp` standard-library logger as well as the `MCPAnalyticsOptions(logger=...)` sink, so they are visible without opting in — silence them with `logging.getLogger("posthog.mcp").setLevel(logging.ERROR)`. Neither fires for stdio, a correctly-wired server, a conversation-anchored session, or the SSE transport (which the mint cannot fix). Documented in the new `posthog/mcp/README.md`.
