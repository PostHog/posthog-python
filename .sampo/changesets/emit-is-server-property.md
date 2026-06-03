---
pypi/posthog: minor
---

Add a configurable `$is_server` event property (default `true`) so PostHog can identify server-side events. Set `is_server=False` when using posthog-python as a client/CLI so the device OS is attributed normally.
