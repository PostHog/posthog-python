---
pypi/posthog: minor
---

Add `metrics={"autocapture": True}` (alpha): HTTP, database and runtime metrics from the official OpenTelemetry instrumentations, with no instrumentation code. Install `posthog[metrics]` and run `opentelemetry-bootstrap -a install` to add the instrumentors for your libraries. Create the client before your web app (for example before `Flask(...)`, or in Django's `AppConfig.ready()`): apps created earlier are not measured. Autocapture does not start when the app already set up OpenTelemetry; export those metrics to PostHog instead. `metrics.before_send` also applies to autocaptured measurements: it can change their value and attributes, or drop them.
