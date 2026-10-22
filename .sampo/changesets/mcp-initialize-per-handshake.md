---
pypi/posthog: patch
---

MCP analytics now sends one `$mcp_initialize` for each `initialize` request that a client sends.

Before, the SDK sent the event on the first request of each session, once per process. A stateless fleet sent one event for each pod that a session reached. A conversation-anchored session sent another event. A client that connected and sent no other request sent no event.

On MCP SDK 2.x, the event comes from `Server.middleware` on every transport. Requests on the 2026-07-28 spec, which has no `initialize`, no longer send one. On MCP SDK 1.x over HTTP, the event comes from `PostHogMcpStatelessSessionMiddleware`. The apps that `instrument()` autowires get this with no change. If you mount the middleware by hand, pass the server as `server=`. MCP SDK 1.x over stdio keeps the first request as the trigger.

The event now also has the handshake request and the server reply as `$mcp_parameters` and `$mcp_response`.
