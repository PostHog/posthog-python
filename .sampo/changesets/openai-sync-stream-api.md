---
pypi/posthog: patch
---

Keep the `openai.Stream` API on sync OpenAI chat completions and Responses streams, so `with`, `close()` and attributes such as `.response` work as they do without PostHog.
