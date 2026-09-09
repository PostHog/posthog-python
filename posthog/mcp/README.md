# PostHog MCP analytics

Product analytics for Model Context Protocol servers. Wrap a Python MCP server so
tool calls, agent intent, resource discovery and reads, and failures are captured
to PostHog as `$mcp_*` events.

Resource bodies are not captured. Captured URLs redact usernames, passwords, and
known credential query parameters, including signed URL credentials. This also
applies when a failed read repeats the URL in its error message. Other query
parameters and fragments can still contain application-specific sensitive data.
Requests and responses keep their original addresses. Use `before_send` to remove
any additional application-specific sensitive data.

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

## Capture the calling model

Model capture is off by default. Enable it for an instrumented MCP Python SDK 1.x or
2.x server:

```python
from posthog.mcp import MCPAnalyticsOptions, instrument

analytics = instrument(
    server,
    posthog,
    MCPAnalyticsOptions(capture_model=True),
)
```

The SDK records the best model identifier visible to the server as
`$mcp_llm_model`. Recognized client metadata wins and sets
`$mcp_llm_model_source` to `client_metadata`. The SDK also adds an `llm_model`
string to each compatible tool schema as a fallback, recorded with source
`self_reported`. It is required for custom dispatchers and the official high-level
MCP SDK adapters. Raw low-level servers and standalone `fastmcp.FastMCP` advertise
it as optional; the standalone adapter strips it before input validation, so
requiring it would reject calls. Existing schema strictness is preserved when
analytics fields are added.

The recognized metadata path is Codex's `x-codex-turn-metadata.model` field in
request `_meta`. Other clients, including Claude Code, use the self-report path
until they expose a stable model field. Missing, blank, and `unknown` values are
not recorded.

MCP does not standardize or attest model identity. Both sources are unverified.
Use them to compare tool behavior across models, not for billing or access
control.

Model self-reporting only runs when PostHog can prove it owns the injected field.
If a tool already declares `llm_model`, or uses a root `$ref`, `oneOf`, `allOf`, or
`anyOf` schema, PostHog leaves the schema and argument untouched. Client metadata
can still be captured in those cases.

For a custom dispatcher, use the same option on `PostHogMCP` and pass request
metadata through explicitly:

```python
from posthog.mcp import PostHogMCP

posthog = PostHogMCP("phc_...", capture_model=True)
tools = posthog.prepare_tool_list(server_tools)
original_tool = next(tool for tool in server_tools if tool["name"] == tool_name)
call = posthog.prepare_tool_call(
    tool_name,
    raw_args,
    request_meta=request.get("params", {}).get("_meta"),
    original_tool=original_tool,
)
result = dispatch(tool_name, call.args)
posthog.capture_tool_call(
    tool_name,
    llm_model=call.llm_model,
    llm_model_source=call.llm_model_source,
)
```

Passing `original_tool` keeps ownership accurate when `tools/list` and
`tools/call` reach different server replicas. A persistent single-process
dispatcher can omit it after calling `prepare_tool_list()`.
Model injection copies tool objects instead of changing their original schemas.
Always advertise the returned list and pass the original application tool to
`prepare_tool_call()`. Repeatedly preparing the original list preserves ownership.

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
