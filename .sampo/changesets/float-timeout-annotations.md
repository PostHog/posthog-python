---
pypi/posthog: patch
---

Annotate `timeout` and `feature_flags_request_timeout_seconds` as `float` on `Client`, `AsyncClient`, `Consumer` and the request helpers, so type checkers accept fractional timeouts such as `timeout=2.5`. The values were already passed straight to `requests` and `httpx`, which take floats, so runtime behaviour is unchanged.
