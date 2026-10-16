---
pypi/posthog: minor
---

Exception autocapture now captures Celery task failures and retry exceptions. `PosthogCeleryIntegration` also captures the exception of a retried task.
