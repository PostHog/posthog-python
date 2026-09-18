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
- `POST /get_feature_flag` - Evaluate a flag locally or remotely
- `POST /reload_feature_flag_definitions` - Fresh, bounded definitions readiness barrier
- `POST /reset` - Reset SDK state

### Key Implementation Details

**Request Tracking**: The adapter monkey-patches `batch_post` to track all HTTP requests made by the SDK, including retries.

**State Management**: Thread-safe state tracking for events captured vs sent, retry attempts, and errors.

**UUID Tracking**: Extracts and tracks UUIDs from batches to verify deduplication.

### Local feature flag evaluation

Both capture adapters advertise `feature_flags_local_evaluation_v1` for harness
**1.1.1**. The capability versions the adapter protocol and tests both legacy and
explicit property matching; it does not change the SDK's default matching mode.

- `/init` maps optional `personal_api_key` to the SDK's `secret_key`. Ordinary
  capture/remote tests do not need it. Background polling is disabled in the
  adapter; explicit reloads still use the real SDK definitions loader.
- `/reload_feature_flag_definitions` takes `timeout_ms` (default 5000, range
  1–30000). It waits for a fresh successful publication, not merely an existing
  snapshot. Failed fetches and authorization/quota resets return `ready: false`.
  A timeout returns HTTP 504; the SDK's in-flight request may finish later on its
  original Client, and another reload on that Client is rejected while it runs.
- `/get_feature_flag` with `only_evaluate_locally: true` uses the SDK's local-only
  result API without emitting flag-called events. A conclusive false has
  `locally_evaluated: true`; an inconclusive result has `value: null`,
  `success: false`, and `locally_evaluated: false`, never remote fallback.
- `force_remote: true` conflicts with local-only mode. When definitions are
  enabled, forced remote calls use a separate definitions-free SDK Client so
  they cannot accidentally resolve from local rules. Legacy remote responses
  and flag-called events are preserved. Reset disposes both Clients.

The adapter is sequential (it does not advertise parallel-test support).

## Documentation

For complete documentation on the test harness and how to implement adapters, see:
- [PostHog SDK Test Harness](https://github.com/PostHog/posthog-sdk-test-harness)
- [Adapter Implementation Guide](https://github.com/PostHog/posthog-sdk-test-harness/blob/main/ADAPTER_GUIDE.md)
