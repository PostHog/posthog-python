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

MCP analytics events report `$lib: "posthog-python-mcp"` and the installed `posthog` package version in `$lib_version`.
Request headers use the same identity and package version, so SDK Health can compare MCP traffic with Python SDK releases.
Because `$lib` is a client-level identity, `instrument()` relabels every event sent by the client passed to it.
Use a client dedicated to MCP analytics if the application also captures unrelated events.

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
factories internally. Nothing extra to add, as long as `instrument()` runs first:

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

### Or skip the middleware entirely: conversation ids

`MCPAnalyticsOptions(enable_conversation_id=True)` derives `$session_id` from the
agent's conversation handle, deterministically and identically on every pod. That
needs no middleware and no ordering discipline, and it is the only thing that
correlates a session under the 2026-07-28 revision's per-request server instances.
Prefer it if you're on a recent client.

### How the SDK tells you it's misconfigured

The failure used to be silent. It now surfaces two ways:

- **At `instrument()`** — if `streamable_http_app()` was already called before
  `instrument()` ran, so the live app has no middleware.
- **At runtime, once** — the first time a tool call arrives over streamable HTTP and the
  session still has to come from this process's memory.

Both go to the logger you pass via `MCPAnalyticsOptions(logger=...)` **and** to the
`posthog.mcp` standard-library logger, so you see them without opting in. Silence them
like any other logger:

```python
logging.getLogger("posthog.mcp").setLevel(logging.ERROR)
```

Neither fires for stdio, for a correctly-wired server, or for a conversation-anchored
session. The instrument-time check can't see whether you added the middleware yourself
(the app is already built by then), so ignore it if you did.

Two gaps worth knowing: jlowin's `fastmcp` 2.x/3.x doesn't expose the attribute the
instrument-time check reads, so those servers get the runtime warning only. And the
deprecated SSE transport is excluded — it keys sessions off a query parameter, and the
mint sets a response header an SSE client never replays, so the middleware wouldn't
help it.
