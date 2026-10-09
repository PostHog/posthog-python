---
pypi/posthog: minor
---

Add a Django REST Framework error-only exception handler that captures handled 5xx API exceptions while preserving DRF responses, leaves expected 4xx errors excluded by default, and consistently inherits Django middleware, client, request-filter, and exception-autocapture configuration.
