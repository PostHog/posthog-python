---
pypi/posthog: patch
---

Record failed OpenAI Responses API stream events as generation errors in the sync and async clients. The original event still reaches the caller, and privacy mode preserves safe provider error codes while redacting the error message.
