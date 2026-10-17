---
pypi/posthog: minor
---

Add an optional `should_capture` callback to `Client`/`Posthog` and module settings for context-based analytics and AI event filtering. It receives no payload and must return `True` to allow capture. Rejected events skip payload cleaning; allowed events avoid the extra cleaning required by `before_send`. Existing `before_send` behavior is unchanged.
