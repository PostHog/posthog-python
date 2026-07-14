---
pypi/posthog: minor
---

Add the `posthog.metrics` API (`count`, `gauge`, `histogram`) — alpha.

Backend services can now record metrics through the same statsd-style pre-aggregating client the browser and Node SDKs ship, with no OpenTelemetry setup:

```python
client = Posthog("<ph_project_api_key>", metrics={"service_name": "billing-worker"})
client.metrics.count("invoices.processed", 1, attributes={"plan": "pro"})
client.metrics.gauge("queue.depth", 42)
client.metrics.histogram("job.duration", 187, unit="ms")
```

Samples aggregate in memory and flush as OTLP/JSON to `/i/v1/metrics` (one data point per series per window, delta temporality). Pending metrics are flushed on `shutdown()`. The `metrics` client option accepts `service_name`, `service_version`, `environment`, `resource_attributes`, `flush_interval`, `max_series_per_flush` (cardinality guardrail, default 1000), and a `before_send` hook.
