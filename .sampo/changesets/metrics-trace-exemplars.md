---
pypi/posthog: minor
---

`client.metrics` now attaches a trace exemplar to each series it flushes. A sample recorded inside a sampled span (a PostHog `start_span` span, or the current OpenTelemetry span) stamps its data point with that span's trace ID, span ID, and value, so metric charts can link to a representative trace. One exemplar per series per flush window; the last sample wins.
