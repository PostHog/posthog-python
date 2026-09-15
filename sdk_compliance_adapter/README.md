# PostHog Python SDK Test Adapter

This Flask adapter exercises the repository's public `posthog.Client` with the
[PostHog SDK Test Harness](https://github.com/PostHog/posthog-sdk-test-harness).
Capture, AI capture, UUID generation, timestamp normalization, retries, flag
results and flag-called events remain SDK-owned.

## Profiles

CI uses harness **1.0.0**, server wire format and sequential execution against
these asynchronous-client profiles. Every capability-selected suite runs;
compliance assertions remain advisory. Each profile has a distinct report artifact.

| Profile | Dockerfile | Encoding when init enables compression | Selected cases |
| --- | --- | --- | --- |
| v0-gzip | `Dockerfile` | gzip | 30 capture + 5 AI + 17 flags = 52 |
| v1-gzip | `Dockerfile.v1` | gzip | 95 capture + 5 AI + 17 flags = 117 |
| v1-deflate | `Dockerfile.v1-deflate` | deflate | 94 capture + 5 AI + 17 flags = 116 |
| v1-zstd | `Dockerfile.v1-zstd` | zstd | 94 capture + 5 AI + 17 flags = 116 |

`CAPTURE_MODE=v1` selects V1 analytics. `CAPTURE_COMPRESSION` selects gzip
(default), deflate or zstd for that process's compression-enabled V1 calls.
Health advertises only that analytics encoding. V1 builds install the SDK's
optional `zstd` extra. V0 analytics and dedicated AI capture use the legacy gzip
boolean; AI always sends to `/i/v0/ai/batch/`, independently of analytics mode.

Init maps `enable_compression: false` (or omission) to explicit
`CaptureCompression.NONE`; true maps to the profile codec. This public argument
takes precedence over `POSTHOG_CAPTURE_COMPRESSION`, so SDK environment settings
cannot silently override harness enable/disable controls. Ordinary tests that
omit the compression setting send uncompressed analytics in every profile.

## Results and applicability

- GeoIP omission preserves the SDK's native `disable_geoip=True`. The unchanged
  `feature_flags.request_payload.disable_geoip_omitted_defaults_to_false` assertion
  expects false and therefore fails in each profile. Explicit overrides still work.
- In V1, `capture_ai.routing.capture_does_not_reroute_ai_named_events` asserts
  `/batch` for an ordinary capture; the SDK correctly uses the configured V1
  analytics endpoint instead. The dedicated AI endpoint is tested separately.
- The pinned mock decodes gzip but not deflate/zstd. Those codec profiles add the
  supported header tests, not full compressed-body/partial-response coverage.
  Their uncompressed tests still exercise the real SDK; a passing header does
  not prove delivery. Gzip's header and decompression tests are capability-gated
  out for deflate/zstd, which accounts for the one-case count difference.
- `sync_mode` uses separate synchronous-per-event sending. The current CLI and
  reusable workflow select only whole suites, not its applicable single-event
  subset. Multi-event queue batching and partial-batch fixtures do not apply;
  this matrix does not claim sync-mode coverage.
- Brotli is not supported. Client-wire cases are not applicable to this server SDK.

Expected assertion totals are **51/52** for V0, **115/117** for V1 gzip and
**114/116** for each additional V1 codec, with the mismatches above retained in
reports rather than skipped.

## Adapter interface

The adapter exposes `/health`, `/init`, `/capture`, `/capture_ai`, `/identify`,
`/flush`, `/get_feature_flag`, `/state` and `/reset`.

Flush uses public `Client.flush(timeout_seconds=None)` to wait for queue drain;
the harness owns the operation timeout. Drain means each event was processed or
permanently failed, not necessarily delivered. Feature flag evaluation also
drains SDK-generated side-effect events before returning.

Transport observers delegate to the original SDK transport. Existing `/state`
counters are diagnostic only: they do not fully account for terminal rejection,
flag side effects or retry statuses. `events_flushed` is a cumulative diagnostic
counter, not a delivery receipt. Wire assertions use the harness mock's requests,
not these counters.

## Local validation

See [CONTRIBUTING.md](CONTRIBUTING.md) for Docker instructions. Native adapter
regression tests also exercise SDK requests against loopback, independently
decode gzip/deflate/zstd, and verify init controls, UUIDs, UTC conversion, AI
routing, native GeoIP configuration and unbounded drain forwarding:

```bash
python -m pip install '.[zstd]' -r sdk_compliance_adapter/requirements.txt pytest
python -m pytest -q sdk_compliance_adapter/test_adapter.py
```

The local Compose harness targets V0 only. To test another profile manually,
build its Dockerfile and run the same harness command against that adapter.
