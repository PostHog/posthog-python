# PostHog Python SDK Test Adapter

This adapter wraps the posthog-python SDK for compliance testing with the [PostHog SDK Test Harness](https://github.com/PostHog/posthog-sdk-test-harness).

## What is This?

This is a simple Flask app that:
1. Wraps the posthog-python SDK
2. Exposes a REST API for the test harness to control
3. Tracks internal SDK state for test assertions

## Running Tests

Tests run automatically in CI via GitHub Actions. See the test harness repo for details.

### Locally with Docker Compose

```bash
# From the posthog-python/sdk_compliance_adapter directory
docker-compose up --build --abort-on-container-exit
```

This will:
1. Build the Python SDK adapter
2. Pull the test harness image
3. Run all compliance tests
4. Show results

### Manually with Docker

```bash
# Create network
docker network create test-network

# Build and run adapter
docker build -f sdk_compliance_adapter/Dockerfile -t posthog-python-adapter .
docker run -d --name sdk-adapter --network test-network -p 8080:8080 posthog-python-adapter

# Run test harness
docker run --rm \
  --name test-harness \
  --network test-network \
  ghcr.io/posthog/sdk-test-harness:latest \
  run --adapter-url http://sdk-adapter:8080 --mock-url http://test-harness:8081

# Cleanup
docker stop sdk-adapter && docker rm sdk-adapter
docker network rm test-network
```

## Adapter Implementation

See [adapter.py](adapter.py) for the implementation.

The adapter implements the standard SDK adapter interface defined in the [test harness CONTRACT](https://github.com/PostHog/posthog-sdk-test-harness/blob/main/CONTRACT.yaml):

- `GET /health` - Return SDK information
- `POST /init` - Initialize SDK with config
- `POST /capture` - Capture an event
- `POST /flush` - Flush pending events
- `GET /state` - Return internal state
- `POST /reset` - Reset SDK state

### Key Implementation Details

**Request Tracking**: The adapter monkey-patches `batch_post` to track all HTTP requests made by the SDK, including retries.

**State Management**: Thread-safe state tracking for events captured vs sent, retry attempts, and errors.

**UUID Tracking**: Extracts and tracks UUIDs from batches to verify deduplication.

## Documentation

For complete documentation on the test harness and how to implement adapters, see:
- [PostHog SDK Test Harness](https://github.com/PostHog/posthog-sdk-test-harness)
- [Adapter Implementation Guide](https://github.com/PostHog/posthog-sdk-test-harness/blob/main/ADAPTER_GUIDE.md)
