---
pypi/posthog: minor
---

feat(exceptions): add opt-in Ed25519 signing of `$exception` events. Set `enable_exception_signing=True` and provide an Ed25519 private key in `exception_signing_private_key`, then register the matching public key in your PostHog project. The SDK signs each captured exception over a canonical projection of its `$exception_list`, so error-tracking ingestion can verify it genuinely came from your backend (rather than being forged through the public ingest key) and mark it verified. Backend use only — never ship a private key in a browser/mobile app. Requires the new `[exception-signing]` extra (`pip install posthoganalytics[exception-signing]`).
