---
pypi/posthog: minor
---

Add distributed tracing (alpha): `start_span()` and `get_active_span()` record spans and export them to PostHog as OTLP, with no OpenTelemetry dependency, when the new `traces` client option is set.
