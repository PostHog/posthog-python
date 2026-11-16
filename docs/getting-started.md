# Getting started

The Python SDK captures events, evaluates feature flags and reports errors from Python 3.10 or later.

To send your first event, install the package, create a client, capture an event, and shut the client down, as the steps below show.

## Install

```bash
pip install "posthog>={major},<{next_major}"
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

Keep one `AsyncPosthog` client for the lifetime of your app. A FastAPI lifespan handler does this.

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

## The posthoganalytics package

`posthoganalytics` is the same SDK under another module name. Use it when another module named `posthog` is on your import path.
