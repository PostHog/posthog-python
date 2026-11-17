---
pypi/posthog: patch
---

Record failed OpenAI Responses API stream events as generation errors in the sync and async clients. The original event still reaches the caller, and privacy mode redacts the provider's error message.
