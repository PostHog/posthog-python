# AGENTS.md

Guidance for coding agents working in `posthog-python`.

## Repo context

- This repository contains the PostHog Python SDK, published as `posthog`.
- The main runtime package is `posthog/`; tests live under `posthog/test/`.
- The project uses `uv` for local development. See `CONTRIBUTING.md` for setup.
- Keep edits targeted and follow existing patterns. Prefer adding or updating tests near the behavior you change.

## Capture protocol (`capture_mode`)

The client supports two ingestion wire protocols, selected by `capture_mode` (precedence: explicit `Client(capture_mode=...)` kwarg > `POSTHOG_CAPTURE_MODE` env var > default).

- `"v0"` (default) — legacy `POST /batch/`. Upgrades stay transparent; existing callers are unaffected.
- `"v1"` — `POST /i/v1/analytics/events`: Bearer auth, a typed event `options` object, per-event results, and partial retry.

v1 request bodies can additionally be compressed via `capture_compression` (precedence: explicit `Client(capture_compression=...)` kwarg > `POSTHOG_CAPTURE_COMPRESSION` env var > the legacy `gzip` flag > none). Supported values are `"none"`, `"gzip"`, `"deflate"` (zlib-wrapped, RFC 1950, to match the server's decoder and the Go/Rust SDKs), and `"zstd"` (requires the optional `posthog[zstd]` extra; explicit zstd without the package raises, env-var zstd warns and falls back). v0 keeps using its own `gzip` flag; `capture_compression` is v1-only.

Where the pieces live:

- `posthog/capture_mode.py` — the `CaptureMode` enum and `_resolve_capture_mode()` precedence logic.
- `posthog/capture_compression.py` — the `CaptureCompression` enum and `_resolve_capture_compression()` precedence logic (with `gzip` fallback).
- `posthog/capture_v1.py` — pure transforms (`_to_v1_event`, `_build_v1_batch_body`) and transport (`_post_v1`, `_compress_v1`, `_parse_v1_response`, `_send_v1_batch`, `CaptureV1Error`).
- Public API surface (enforced by `references/public_api_snapshot.txt`): `CaptureMode`, `CaptureCompression` (both re-exported from `posthog`), `CaptureV1Error`, and the two env var names. Everything else in these modules is underscore-private plumbing.
- Routing: `Consumer._send_analytics` (async) and `Client._enqueue` (sync) pick the analytics submitter by `capture_mode`. The dedicated `$ai_*` endpoint has no v1 form and always uses the legacy submitter.

v1-specific behavior to preserve when editing: sentinel `$`-properties are lifted into `options` (coerced to native JSON types or omitted — a wrong type 400s the whole batch); top-level `$set`/`$set_once` are relocated into `properties`; only events the server tags `retry` are resent (stable `PostHog-Request-Id`/`created_at`, incrementing `PostHog-Attempt`); a server `drop` is a terminal per-event rejection — drops are accumulated across attempts and surfaced via `CaptureV1Error`/`on_error` even on a 2xx with no retries (a success status is not full delivery); `Retry-After` is a *minimum*, not a replacement (the client waits `max(configured_backoff, min(Retry-After, _MAX_BACKOFF_SECONDS))`); `_MAX_BACKOFF_SECONDS` (30s) is the single ceiling for both the exponential backoff and the `Retry-After` clamp; `429` is terminal.

Retry blocking matches v0: in the default async mode retries happen on the background consumer thread, but with `sync_mode=True` the partial-retry loop (including its backoff sleeps) runs inline on the calling thread, so a slow/erroring endpoint blocks the caller until retries are exhausted.

## Validation

Useful checks:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy --no-site-packages --config-file mypy.ini . | uv run mypy-baseline filter
uv run pytest --verbose --timeout=30
uv run python -W error -c "import posthog"
```

For focused changes, run the smallest relevant `uv run pytest ...` command first.

If public API surface changes, update/check `references/public_api_snapshot.txt` with:

```bash
make public_api_snapshot
make public_api_check
```

## `posthoganalytics` mirror package

This repo also publishes `posthoganalytics`, a generated mirror of `posthog` used by the PostHog app. The mirror is created by copying `posthog/` to `posthoganalytics/` and rewriting absolute imports such as `from posthog.foo import ...` to `from posthoganalytics.foo import ...`.

Important when editing SDK-internal code:

- Prefer relative imports for imports within the SDK package, especially in runtime modules under `posthog/`.
  - Good: `from .client import Client`, `from .exception_utils import extract_exception_properties`
  - Risky: `from posthog.client import Client`
- Absolute `posthog...` imports inside SDK modules can break the `posthoganalytics` mirror when it is imported inside an application that also has its own `posthog` package/module on `sys.path`.
- Test mirror-sensitive changes by running the normal focused tests and, when relevant, `make prep_local` to generate a local `posthoganalytics` copy for testing in the PostHog app.
- Do not commit generated `posthoganalytics/` directories; they are build/local artifacts.

## Release/build notes

- `make build_release` builds the `posthog` distribution.
- `make build_release_analytics` builds the `posthoganalytics` distribution and temporarily rewrites/copies package files; ensure the working tree is clean before and after running it.
- Release flow publishes both packages; see `RELEASING.md`.
