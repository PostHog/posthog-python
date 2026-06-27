---
pypi/posthog: minor
---

Add an opt-in `capture_mode` for the Capture V1 ingestion protocol (`POST /i/v1/analytics/events`). Set `capture_mode="v1"` on the client (or the `POSTHOG_CAPTURE_MODE=v1` environment variable) to use Bearer auth, per-event results, and partial retry. Defaults to `"v0"` (the legacy `/batch/` endpoint), so existing setups are unaffected.

When using `capture_mode="v1"`, request bodies can be compressed via `capture_compression` (or `POSTHOG_CAPTURE_COMPRESSION`): `"gzip"`, `"deflate"`, or `"none"` (default). The legacy `gzip=True` flag is honored as a fallback.
