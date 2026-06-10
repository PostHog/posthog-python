---
pypi/posthog: minor
---

Warn when an AI wrapper's `base_url` points at the PostHog AI Gateway, which would otherwise capture and bill each LLM generation twice (once by the wrapper, once by the gateway). The wrapper only warns and never drops the event.
