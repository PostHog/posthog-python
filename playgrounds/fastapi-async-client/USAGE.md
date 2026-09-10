# FastAPI Async Client Playground

Demonstrates the asyncio-native PostHog client in a FastAPI application:

- buffered `capture()`
- awaited `capture_immediate()`
- `set()`, `set_once()`, `group_identify()`, and `alias()`
- exception capture
- awaited feature flag evaluation and remote config
- explicit flush and graceful shutdown

## Quick start

```bash
cd playgrounds/fastapi-async-client
export POSTHOG_API_KEY="phc_..."
export POSTHOG_HOST="https://us.i.posthog.com"
# Required only by the remote-config endpoint:
export POSTHOG_SECRET_KEY="phx_..."
uv sync
uv run python main.py
```

Open <http://127.0.0.1:8000/docs> for the interactive API documentation.

## Try the APIs

Buffered capture queues an event and returns without waiting for the network:

```bash
curl -X POST http://127.0.0.1:8000/capture/user-123
```

Immediate capture bypasses the queue and waits for the delivery attempt:

```bash
curl -X POST http://127.0.0.1:8000/capture-immediate/user-123
```

Queue person and group updates:

```bash
curl -X POST http://127.0.0.1:8000/identify/user-123
curl -X POST http://127.0.0.1:8000/group/company-123
curl -X POST 'http://127.0.0.1:8000/alias?previous_id=anonymous-123&distinct_id=user-123'
```

Evaluate flags remotely, read the returned snapshot synchronously, and attach that
snapshot to a capture event:

```bash
curl 'http://127.0.0.1:8000/flags/user-123?flag_key=async-client-demo'
```

Fetch remote config using `POSTHOG_SECRET_KEY`:

```bash
curl http://127.0.0.1:8000/remote-config/example-config
```

Capture an exception or explicitly flush buffered events:

```bash
curl http://127.0.0.1:8000/test-exception
curl -X POST http://127.0.0.1:8000/flush
```

Stopping the application triggers the FastAPI lifespan cleanup, which awaits
`AsyncPosthog.shutdown()` to flush queued events and close the HTTP transport.
