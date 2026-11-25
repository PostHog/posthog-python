---
pypi/posthog: patch
---

MCP analytics sessions now follow the transport rules of the sdk-specs "Session identity" requirement:

- Every sessionless HTTP request gets a `$session_id` of its own. That covers a stateless server without the session middleware, and a request that sends no `Mcp-Session-Id` after another client's request did. Before, these requests shared the process session with every other caller, and an unwired stateless route reported each pod's first request as an `$mcp_initialize`. That fallback is gone, so such a server reports one session per request until the middleware is added. The runtime warning names the fix, and no longer fires for 2026-07-28 requests, which no middleware can group.
- A call's events all use the session resolved when the call arrived, so a stdio call longer than 30 minutes no longer changes session midway.
- A one-client connection's session rotates once 30 or more minutes pass between the end of one tool call and the arrival of the next. A `tools/list` or resource request no longer keeps the session alive.
- The deprecated SSE transport groups a connection's requests by its `session_id` query parameter, read only on a connection that completed a handshake.
