---
pypi/posthog: patch
---

MCP analytics now surfaces the previously-silent case where the stateless session mint middleware (`PostHogMcpStatelessSessionMiddleware`) never attached — the trap where an ASGI app is built or mounted before `instrument()` runs, so autowiring can't retrofit it and every session falls back to a fragmented per-process id. `instrument()` now warns when `streamable_http_app()` was already called before it ran, and a one-time runtime warning fires the first time a tool call arrives over HTTP with no session id. Both point to the manual fix (`app.add_middleware(PostHogMcpStatelessSessionMiddleware)`), which is now documented in `posthog/mcp/README.md`. No behavior change on correctly-wired servers or stdio.
