---
pypi/posthog: patch
---

Streamed Anthropic generations no longer stack output tokens when a stream emits more than one `message_delta` (server tools can send several). Each delta's usage is cumulative, so the accumulator now keeps the latest total instead of adding every delta together.
