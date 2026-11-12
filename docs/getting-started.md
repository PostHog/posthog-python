# Getting started

The Python SDK captures events, evaluates feature flags and reports errors from Python 3.10 or later.

To send your first event, install the package, create a client, capture an event, and shut the client down, as the steps below show.

## Install

```bash
pip install posthog
```

## Initialize

Create one client for your app with your project token and host. Your project token and host are in your [project settings](https://app.posthog.com/project/settings). Keep the token in an environment variable rather than in code.

```python
from posthog import Posthog

posthog = Posthog("<ph_project_token>", host="<ph_client_api_host>")
```

## Capture an event

```python
posthog.capture("user_signed_up", distinct_id="user_123", properties={"plan": "free"})
```

`capture` queues the event and returns right away. A background thread sends it.

## Shut down

Call `shutdown()` before your process exits, so queued events are sent.

```python
posthog.shutdown()
```

In an asyncio app, use `AsyncPosthog` as an async context manager instead. See [AsyncClient](#asyncclient).

## Use AsyncPosthog in FastAPI

Keep one `AsyncPosthog` client for the lifetime of your app. A FastAPI lifespan handler does this. Leaving the `async with` block calls `shutdown()`.

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI
from posthog import AsyncPosthog

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncPosthog("<ph_project_token>", host="<ph_client_api_host>") as posthog:
        app.state.posthog = posthog
        yield

app = FastAPI(lifespan=lifespan)
```

## Add the ASGI middleware

`PosthogASGIMiddleware` creates a context for each HTTP or WebSocket request and captures exceptions that escape the app. In FastAPI or Starlette, add it to the app:

```python
from posthog.integrations.asgi import PosthogASGIMiddleware

app.add_middleware(PosthogASGIMiddleware, client=posthog)
```

For Litestar, another ASGI framework or a raw ASGI app, wrap the app instead:

```python
app = PosthogASGIMiddleware(app, client=posthog)
```

## TCP keepalive defaults

`enable_keep_alive()` uses a 60 second idle time, a 60 second probe interval and 3 probes before timeout.

## The posthoganalytics package

`posthoganalytics` is the same SDK under another module name. Use it when another module named `posthog` is on your import path.

## Tracing details

- The active span is tracked with `contextvars`, so it carries across `await`. Threads don't reliably inherit it. Pass `parent=span` in a thread or a `ThreadPoolExecutor` task.
- A malformed `traceparent` starts a new trace and doesn't raise.
- A continued trace propagates the sampled flag it received. PostHog doesn't sample, so the span is exported whichever way the flag is set.
- PostHog clamps a `start_time` more than 24 hours old to the receive time.
- A recorded exception's stack trace contains file paths from your server. Remove `exception.stacktrace` in `before_span_send` to keep them in your process.
- A script that exits without `shutdown()` gets a brief best-effort span flush. Don't rely on it.

The `before_span_send` hook follows these rules:

- The span dict has `name`, `kind`, `status`, `attributes`, `events`, `start_time_ns`, `end_time_ns`, `trace_id`, `span_id` and `parent_span_id`. Attributes are plain Python values.
- The hook runs after `posthogDistinctId` and `sessionId` are attached, so it can scrub them.
- The hook must be a regular function. An `async` hook drops every span.
- In a list of hooks, the first one to return `None` stops the chain.
- An entry that isn't callable turns tracing off for the client.

The span limits follow these rules:

- Past a cap, the earliest attributes and events are kept. The span reports how many were dropped.
- `posthogDistinctId` and `sessionId` don't count toward the attribute cap and are never dropped.
- An `exception` event the SDK records uses an event slot. A span that fills its events and then raises keeps its `error` status and loses the exception detail.
- The value length cap also applies to strings nested in lists and dictionaries.
- All caps are applied again after `before_span_send`.
- At `max_live_spans`, `start_span` returns a span that isn't recorded. Spans open longer than `max_span_age` seconds are then treated as leaked and never exported.
- Spans beyond `max_queue_size` are dropped.
- An invalid `traces` value falls back to its default with a warning.
