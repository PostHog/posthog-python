---
pypi/posthog: minor
---

Add an opt-in `capture_mode` for the Capture V1 ingestion protocol (`POST /i/v1/analytics/events`). Set `capture_mode="v1"` on the client (or the `POSTHOG_CAPTURE_MODE=v1` environment variable) to use Bearer auth, per-event results, and partial retry. Defaults to `"v0"` (the legacy `/batch/` endpoint), so existing setups are unaffected.

When using `capture_mode="v1"`, request bodies can be compressed via `capture_compression` (or `POSTHOG_CAPTURE_COMPRESSION`): `"gzip"`, `"deflate"`, `"zstd"` (requires the optional `posthog[zstd]` extra), or `"none"` (default). The legacy `gzip=True` flag is honored as a fallback.

Per-event server verdicts are surfaced through the existing `on_error` handler: events the backend explicitly drops, or fails to accept after retries, raise a `CaptureV1Error` carrying the affected event UUIDs — so a rejection is never silently lost, even when the HTTP request itself succeeded.
