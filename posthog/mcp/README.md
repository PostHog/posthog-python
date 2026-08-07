# PostHog MCP analytics

Product analytics for Model Context Protocol servers. Wrap a Python MCP server so
every tool call, agent intent, and failure is captured to PostHog as a `$mcp_*` event.

```python
from posthog import Posthog
from posthog.mcp import instrument
from mcp.server.fastmcp import FastMCP

posthog = Posthog("phc_...", host="https://us.i.posthog.com")
server = FastMCP("my-server")
analytics = instrument(server, posthog)
```

Install is just `pip install posthog`. `instrument()` needs the MCP SDK at runtime,
but anyone wrapping a server already has it.

## Stateless / multi-pod servers

A stateless MCP server issues no session id, so `$session_id` fragments across pods
and the client identity (sent only at `initialize`) is lost. PostHog fixes this with
a small ASGI middleware — `PostHogMcpStatelessSessionMiddleware` — that mints a
self-encoded token onto the `Mcp-Session-Id` response header at `initialize`; the
client replays it on every request, so any pod recovers the session and harness from
the header alone.

### Zero-config path (recommended)

`instrument()` wraps the FastMCP server's app factories (`streamable_http_app()` /
`sse_app()`), so an app you build **after** calling `instrument()` already carries the
middleware — including `mcp.run(transport="streamable-http")`, which calls those
factories internally. Nothing extra to add:

```python
server = FastMCP("my-server", stateless_http=True)
instrument(server, posthog)
server.run(transport="streamable-http")   # already wired
```

### Manual path — required when you build the app yourself

Autowiring only affects an app built **after** `instrument()` runs. If you build or
mount the ASGI app before `instrument()`, or in a different module — the common
FastAPI case — the running app gets **no** middleware and every session falls back to
a fragmented per-process id. Add the middleware to your app explicitly:

```python
from posthog.mcp import PostHogMcpStatelessSessionMiddleware, get_mcp_session

app = mcp.streamable_http_app()
app.add_middleware(PostHogMcpStatelessSessionMiddleware)
```

This is also the path for a custom `PostHogMCP` dispatcher (you own the ASGI app),
where you then read the recovered session per request:

```python
sess = get_mcp_session(request)   # sess.session_id, sess.client_name, ...
```

### How the SDK tells you it's misconfigured

The failure used to be silent. It now surfaces two ways:

- **At `instrument()`** — if `streamable_http_app()` was already called before
  `instrument()` ran (so the live app has no middleware), a warning is logged.
- **At runtime** — the first time a tool call arrives over HTTP with no session id and
  PostHog falls back to a per-process `generated` session, a one-time warning is logged.

Both point back to `app.add_middleware(PostHogMcpStatelessSessionMiddleware)`. Warnings
go through the logger you pass via `MCPAnalyticsOptions(logger=...)`.
