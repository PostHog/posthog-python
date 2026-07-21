---
'pypi/posthog': minor
---

The `client.metrics` config can now be set through module-level settings: assign `posthog.metrics = {"service_name": ..., ...}` alongside `posthog.api_key` and the dict is applied when `setup()` builds the global client. Previously module-configured apps had no way to pass the metrics config, so every series recorded through the global client shipped `service.name='unknown_service'`. Late assignment (e.g. a Django `ready()` hook running after an early `setup()`) still applies on the next `setup()` call, as long as the metrics API hasn't been used yet.
