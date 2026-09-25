# PostHog MCP analytics

Product analytics for Model Context Protocol servers. Wrap a Python MCP server so
tool calls, agent intent, resource discovery and reads, and failures are captured
to PostHog as `$mcp_*` events.

Resource bodies are not captured. Resource and resource-template listings are:
a listing is metadata (names, uris, mime types), so `$mcp_resources_list` carries
it as `$mcp_response`.

Captured URLs redact usernames, passwords, and credential-named query and
fragment parameters, including signed URL credentials. This applies to every
captured string, tool call parameters, responses and error messages included, so
it also covers a failed read that repeats the URL in its error message. URLs
longer than 8,192 characters or with more than 128 query fields are redacted
entirely to bound parsing work. Other query and fragment parameters can still
contain application-specific sensitive data. Requests and responses keep their
original addresses. Use `before_send` to remove any additional
application-specific sensitive data.

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

## Defaults and opt-outs

A valid echoed conversation handle takes precedence over transport sessions. A request that already carries a transport session or a PostHog session token does not mint a new handle or append a prompt-back block. Requests carrying neither use the conversation fallback.


Intent, model capture, conversation correlation, and MCP exception capture are on by default.
Missing-capability reporting and feedback collection remain off.

```python
from posthog.mcp import MCPAnalyticsOptions, instrument

instrument(server, posthog, MCPAnalyticsOptions(capture_model=False, enable_conversation_id=False))
```

Model capture adds an `llm_model` argument to compatible tool schemas, required on the official
high-level adapters and optional elsewhere. Dispatch never enforces it, so servers keep working;
strict-schema clients see the new field. Set `capture_model=False` to leave schemas untouched.
Conversation correlation adds an optional `conversation_id` argument and returns a handle in
eligible tool results. Clients must echo it to group later calls; calls without it mint new handles.
Set `enable_conversation_id=False` to retain transport-based session grouping and unchanged
response content. Custom `PostHogMCP` dispatchers also enable model capture and conversation
correlation by default. The reasoning is recorded in posthog-js `docs/adr/0013`.

## Capture the calling model

Model capture is on by default for instrumented MCP Python SDK 1.x and 2.x servers.
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

A raw low-level server learns ownership while serving `tools/list`. A fresh instance that never
served one has no answer, so it records `llm_model` as the self-reported model and strips
nothing (posthog-js ADR-0011: reads fail open, strips fail closed). A tool that declares its own
`llm_model` on such an instance is therefore recorded under `$mcp_llm_model` until a listing says
otherwise; `capture_model=False` or `before_send` are the escapes. High-level adapters and
standalone `fastmcp.FastMCP` read ownership from the registered tool schema, so they are
unaffected and need no prior listing when dispatch uses that registry.

On MCP SDK 1.x, standalone FastMCP application middleware can replace or reroute a tool.
When it overrides a listing or dispatch hook, PostHog does not inject `llm_model`: a fresh
replica cannot safely distinguish that field from a replacement tool's own argument.
Client metadata capture remains enabled. This also applies to pass-through logging or
authorization middleware with those hooks; argument-based model capture is skipped so an
application-owned value cannot be mistaken for analytics. No additional catalog lookup runs
during a tool call.

For a custom dispatcher, `PostHogMCP` enables model capture and conversation correlation
by default. `prepare_tool_list()` injects the analytics fields and records ownership by tool
name. `prepare_tool_call()` removes SDK-owned arguments and resolves the conversation and
session. `prepare_tool_result()` returns the result to send and the final values to capture:

```python
from posthog.mcp import PostHogMCP

posthog = PostHogMCP("phc_...")
tools = posthog.prepare_tool_list(server_tools)
original_tool = next(tool for tool in server_tools if tool["name"] == tool_name)
call = posthog.prepare_tool_call(
    tool_name,
    raw_args,
    request_meta=request.get("params", {}).get("_meta"),
    original_tool=original_tool,
    session_id=transport_session_id,
)
prepared = posthog.prepare_tool_result(dispatch(tool_name, call.args), call)
posthog.capture_tool_call(
    tool_name,
    llm_model=call.llm_model,
    llm_model_source=call.llm_model_source,
    session_id=prepared.session_id,
    conversation_id=prepared.conversation_id,
)
return prepared.result
```

Passing `original_tool` keeps ownership accurate when `tools/list` and
`tools/call` reach different server replicas. A persistent single-process
dispatcher can omit it after calling `prepare_tool_list()`.
Model and conversation injection copy tool objects instead of changing their original schemas.
Always advertise the returned list and pass the original application tool to
`prepare_tool_call()`. Repeatedly preparing the original list preserves ownership.

Pass an existing transport or request session as `session_id`. A valid echoed
`conversation_id` takes precedence. Otherwise the existing session stays, and no new handle is
minted. `prepare_tool_result()` appends a new handle to the result's `content` and mirrors it
into `structuredContent` when the tool declares an output schema. If the result has no channel
that can carry a new handle, `conversation_id` is `None` and the derived `session_id` is kept.
Set `enable_conversation_id=False` on `PostHogMCP` to keep the previous custom-dispatcher
behavior.

## Collect agent feedback

Feedback collection is off by default. Enable it to advertise a `send_feedback`
virtual tool. Agents use it to report a missing capability (the priority
category), a tool that failed or confused them, or praise:

```python
from posthog.mcp import MCPAnalyticsOptions, instrument

analytics = instrument(
    server,
    posthog,
    MCPAnalyticsOptions(collect_feedback=True),
)
```

Each call emits one `$mcp_feedback` event (never a `$mcp_tool_call`) with
`$mcp_feedback_type`, `$mcp_feedback_summary`, `$mcp_feedback_details`,
`$mcp_feedback_friction_points`, `$mcp_feedback_suggested_improvement`,
`$mcp_feedback_tool`, `$mcp_feedback_sentiment`, and
`$mcp_feedback_task_completed`. The summary and details also map to
`$mcp_intent`. Free-text fields are sanitized, PII-redacted, and length-bounded;
the raw arguments are never captured. An invalid `feedback_type` falls back to
`other`. The agent receives an honest acknowledgement: the call records feedback
and adds no tools.

The tool covers what `report_missing` covers (as feedback_type
`missing_capability`), so new integrations should enable only one of the two.

See [Virtual tools on a paginated `tools/list`](#virtual-tools-on-a-paginated-toolslist)
for name collisions and the page rule.

Use the object form to rename the tool, declare host-specific fields, or route
reports to a real backend:

```python
from posthog.mcp import CollectFeedbackOptions, MCPAnalyticsOptions

options = MCPAnalyticsOptions(
    collect_feedback=CollectFeedbackOptions(
        extra_properties={
            "product_area": {
                "type": "string",
                "description": "The product the feedback is about.",
            },
        },
        extra_required=["product_area"],
        on_feedback=lambda report: feedback_backend.record(report),
    ),
)
```

Declared extras merge into the advertised schema and are captured as
`$mcp_feedback_<key>`. Arguments the agent invents are never captured; the
handler reads them from `report.raw`. A key that collides with a core field
raises at configuration time. `on_feedback` may be sync or async; a returned
non-blank string replaces the default reply, and a raised handler is logged and
falls back to it. The event is captured either way.

For a custom dispatcher, use the same option on `PostHogMCP`:

```python
from posthog.mcp import PostHogMCP, send_feedback_result

posthog = PostHogMCP("phc_...", collect_feedback=True)

# tools/list handler
tools = posthog.prepare_tool_list(server_tools, collect_feedback=True)

# tools/call dispatcher
call = posthog.prepare_tool_call(tool_name, raw_args)
if call.is_feedback:
    # Replies to the agent and stops dispatch.
    prepared = posthog.prepare_tool_result(send_feedback_result(), call)
    posthog.capture_feedback(  # emits $mcp_feedback
        report=call.feedback_report,
        session_id=prepared.session_id,
        conversation_id=prepared.conversation_id,
    )
    return prepared.result
```

`on_feedback` is ignored on this path — the dispatcher routes reports itself via
`call.feedback_report`.

On this path you own the page rule, because you pass the switches per call. Pass
them for the first page only, and pass your own tool as `original_tool` so a real
tool by a virtual tool's name wins:

```python
first_page = request.params.get("cursor") is None
tools = posthog.prepare_tool_list(
    page_tools, report_missing=first_page, collect_feedback=first_page
)

call = posthog.prepare_tool_call(
    tool_name, raw_args, original_tool=my_tools.get(tool_name)
)
```

## Virtual tools on a paginated `tools/list`

`instrument()` advertises up to two tools of its own: `get_more_tools`
(`report_missing=True`) and `send_feedback` (`collect_feedback=True`).

A client concatenates every `tools/list` page into one list, so each virtual tool
is appended to the **first page only** — the page every client reads, including
clients that never follow `nextCursor`. "First page" means a request with no
cursor; an empty string is a valid opaque cursor, so `cursor: ""` is a
continuation page.

### Tool name collisions

Your tools win when the SDK can see them. Rename the SDK's to keep both:

```python
MCPAnalyticsOptions(
    report_missing=True,
    missing_capability_tool_name="find_posthog_tools",
    collect_feedback=CollectFeedbackOptions(tool_name="tell_posthog"),
)
```

- A real tool using the name **on the first page** wins: the SDK warns, injects
  nothing, and never intercepts yours.
- A real tool that appears **only on a later page** is shadowed — page one cannot
  see page two, so the SDK's tool is already advertised and calls to the name
  reach it. The SDK warns when that page is served. `@posthog/mcp` is the same.
- Configuring **both** virtual tools with one name advertises only
  `get_more_tools`, and warns.

Warnings go to the `logger` option *and* the `posthog.mcp` standard-library
logger, so a default-configured host sees them on stderr.

At call time the SDK checks ownership again, covering the process that serves a
call without having served a listing — the ordinary multi-pod case. FastMCP and
v2 `MCPServer` are asked via their tool registry; a raw low-level server has
none, so the SDK calls your own `tools/list` handler, once per call to a virtual
tool's name and never for ordinary traffic. If that check cannot answer — a
registry lookup or a listing handler raised, or you registered `tools/list`
after `instrument()` — the call is delegated to your server rather than
intercepted, and the reason is logged: guessing the other way would swallow a
real tool of yours silently.

This check is the only ownership signal used at call time, so a server that
serves **different tool sets to different callers** from one instrumented
instance is handled correctly: nothing is carried over from whichever listing
happened to be served last.

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

`enable_conversation_id` is on by default and derives `$session_id` from the
agent's conversation handle, deterministically and identically on every pod. That
needs no middleware and no ordering discipline, and it is the only thing that
correlates a session under the 2026-07-28 revision's per-request server instances.
Prefer it if you're on a recent client.

The `get_more_tools` and `send_feedback` virtual tools also use the conversation
handle when this option is enabled.

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

Standalone `fastmcp` 4 uses the MCP SDK v2 handler registry. `instrument()` detects
that registry automatically and captures tool calls over stdio and streamable HTTP,
including the stateless protocol. Mounted tools retain their own arguments; analytics
parameters are removed before dispatch only when the tool does not declare them.
Instrumenting both the wrapper and its underlying server works in either order.
For versioned tools, argument ownership follows the version requested by the client.
Each tool call resolves the schema through FastMCP's tool listing in the current request context, including middleware and session transforms.
This adds a schema lookup per call so clients with different tool schemas cannot change how another client's arguments are handled.
The same installation code continues to support standalone FastMCP 2.x/3.x on MCP SDK v1.

Two gaps worth knowing: jlowin's `fastmcp` 2.x/3.x doesn't expose the attribute the
instrument-time check reads, so those servers get the runtime warning only. And the
deprecated SSE transport is excluded — it keys sessions off a query parameter, and the
mint sets a response header an SSE client never replays, so the middleware wouldn't
help it.
