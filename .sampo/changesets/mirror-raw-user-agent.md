---
"pypi/posthog": patch
---

Django middleware also sends the request user agent as `$raw_user_agent`, the standardized property PostHog's server-side classification (e.g. bot detection) reads
