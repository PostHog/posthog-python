---
pypi/posthog: minor
---

Add a Django REST Framework exception handler integration that automatically captures handled 5xx API exceptions while preserving DRF responses, leaving expected 4xx errors excluded by default, and honoring exception-capture opt-outs.
