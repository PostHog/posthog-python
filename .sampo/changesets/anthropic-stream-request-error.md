---
pypi/posthog: patch
---

`posthog.ai.anthropic`: `messages.stream()` (sync and async) now captures an `$ai_generation` event with `$ai_is_error` when the initial request fails, for example with a 401 or a connection error. Before, only `messages.create(stream=True)` captured that failure, so errors raised on entering the `stream()` context manager were never reported.
