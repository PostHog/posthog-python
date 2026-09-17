---
pypi/posthog: minor
---

Add the `network` option to the `metrics` client config. When set, the SDK records the duration of every HTTP request the application makes with `requests` or `httpx` as the `http.client.request.duration` histogram, with `method`, `host`, templated `path` and `status_class` attributes. `name` and `attributes` functions customise what is recorded. The SDK's own requests are skipped, and the wrappers are removed on `shutdown()`.
