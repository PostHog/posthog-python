# Capture protocol reference

Read this before changing capture configuration, serialization, routing, or retries. This documents the Python implementation; it is not a substitute for checking applicable [sdk-specs](https://github.com/PostHog/sdk-specs), nor a claim that a published spec defines every v1 detail below.

## Configuration and wire formats

The analytics client supports two ingestion wire protocols, selected by `capture_mode` (precedence: explicit `Client(capture_mode=...)` kwarg > `POSTHOG_CAPTURE_MODE` env var > default).

- `"v0"` (default) — legacy `POST /batch/`. Upgrades stay transparent; existing callers are unaffected.
- `"v1"` — `POST /i/v1/analytics/events`: Bearer auth, a typed event `options` object, per-event results, and partial retry.

v1 request bodies can additionally be compressed via `capture_compression` (precedence: explicit `Client(capture_compression=...)` kwarg > `POSTHOG_CAPTURE_COMPRESSION` env var > the legacy `gzip` flag > none). Supported values are `"none"`, `"gzip"`, `"deflate"` (zlib-wrapped, RFC 1950, to match the server's decoder and the Go/Rust SDKs), and `"zstd"` (requires the optional `posthog[zstd]` extra; explicit zstd without the package raises, env-var zstd warns and falls back to the legacy `gzip` flag or none). v0 keeps using its own `gzip` flag; `capture_compression` is v1-only.

## Implementation and API map

- [`posthog/capture_mode.py`](../posthog/capture_mode.py) — the `CaptureMode` enum and `_resolve_capture_mode()` precedence logic.
- [`posthog/capture_compression.py`](../posthog/capture_compression.py) — the `CaptureCompression` enum and `_resolve_capture_compression()` precedence logic (with `gzip` fallback).
- [`posthog/capture_v1.py`](../posthog/capture_v1.py) — pure transforms (`_to_v1_event`, `_build_v1_batch_body`) and transport (`_post_v1`, `_compress_v1`, `_parse_v1_response`, `_send_v1_batch`, `CaptureV1Error`).
- Public API surface (enforced by [`references/public_api_snapshot.txt`](../references/public_api_snapshot.txt)): `CaptureMode`, `CaptureCompression` (both re-exported from `posthog`), `CaptureV1Error`, and the env var constants `CAPTURE_MODE_ENV_VAR` / `CAPTURE_COMPRESSION_ENV_VAR` naming the two env vars above. Everything else in these modules is private plumbing, not an additional public API.
- Routing: [`Consumer.request`](../posthog/consumer.py) (async) and [`Client._enqueue`](../posthog/client.py) (sync) pick the submitter by the lane's `capture_mode`. The analytics lane uses the client configuration; the separate AI lane is pinned to v0 and its own endpoint.

## v1 serialization and delivery safeguards

- Sentinel `$`-properties are lifted into `options` (coerced to native JSON types or omitted — a wrong type 400s the whole batch).
- Top-level `$set`/`$set_once` are relocated into `properties`.
- After a per-event response, only events the server tags `retry` are resent. Keep `PostHog-Request-Id` / `created_at` stable across attempts and increment `PostHog-Attempt`. Transport failures and retryable HTTP failures retry the pending batch.
- A server `drop` is a terminal per-event rejection. Drops are accumulated across attempts and surfaced via `CaptureV1Error` / the consumer's `on_error` even on a 2xx with no retries or when later retries succeed: a success status is not full delivery.
- `Retry-After` is a *minimum*, not a replacement: wait `max(configured_backoff, min(Retry-After, _MAX_BACKOFF_SECONDS))` for a positive header. `_MAX_BACKOFF_SECONDS` (30s) is the single ceiling for both the exponential backoff and the `Retry-After` clamp.
- `429` is terminal in v1.

Retry blocking matches v0: in the default async mode retries happen on the background consumer thread, but with `sync_mode=True` the partial-retry loop (including its backoff sleeps) runs inline on the calling thread, so a slow/erroring endpoint blocks the caller until retries are exhausted.

## Relevant tests

Review the existing cases in [`test_capture_mode.py`](../posthog/test/test_capture_mode.py), [`test_capture_compression.py`](../posthog/test/test_capture_compression.py), and [`test_capture_v1.py`](../posthog/test/test_capture_v1.py) for precedence, optional zstd, typed serialization, partial retries, request identity, drops, terminal statuses, and bounded backoff. Routing cases also live in [`test_client.py`](../posthog/test/test_client.py) and [`test_consumer.py`](../posthog/test/test_consumer.py). Follow [contributor validation guidance](../CONTRIBUTING.md#ci-aligned-checks), starting with the smallest relevant tests.
