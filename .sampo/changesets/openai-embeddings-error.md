---
pypi/posthog: patch
---

Capture an `$ai_embedding` event with `$ai_is_error` and `$ai_error` when an OpenAI `embeddings.create()` call fails, in the sync and async wrappers. Before, a failed embeddings call left no event.
