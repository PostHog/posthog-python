---
pypi/posthog: minor
---

Add `metrics={"autocapture": True}`: HTTP, database and runtime metrics from the official OpenTelemetry instrumentations, with no instrumentation code. Install `posthog[metrics]` and run `opentelemetry-bootstrap -a install` to add the instrumentors for your libraries.
