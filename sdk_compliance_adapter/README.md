# PostHog Python SDK Test Adapter

This adapter wraps the posthog-python SDK for compliance testing with the [PostHog SDK Test Harness](https://github.com/PostHog/posthog-sdk-test-harness).

## What is This?

This is a simple Flask app that:
1. Wraps the posthog-python SDK
2. Exposes a REST API for the test harness to control
3. Tracks internal SDK state for test assertions

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for local build and compliance test instructions.

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
