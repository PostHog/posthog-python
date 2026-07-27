---
pypi/posthog: minor
---

feat(mcp): stateless and multi-pod server support — carry `$session_id` and the client identity (harness) across pods via a self-encoded `Mcp-Session-Id` token minted at `initialize` and replayed on every request. Auto-wired on the `instrument()` FastMCP path (`stateless_http=True`); custom `PostHogMCP` dispatchers add `PostHogMcpStatelessSessionMiddleware` and read `get_mcp_session()`.
