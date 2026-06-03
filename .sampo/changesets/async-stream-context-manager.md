---
pypi/posthog: patch
---

Fix `TypeError: 'async_generator' object does not support the asynchronous context manager protocol` when using the async AI wrappers (`posthog.ai.openai.AsyncOpenAI`, `posthog.ai.anthropic.AsyncAnthropic`, `posthog.ai.gemini.AsyncClient`) with libraries such as pydantic-ai that consume streaming responses via `async with`. Streaming responses now support both `async for` and `async with`, and exiting the context closes the underlying provider stream.

Note: the async streaming return type changes from a bare `AsyncGenerator` to `AsyncStreamWrapper` (`async for`, `await response.aclose()`, and attribute access like `.response` are preserved, but `inspect.isasyncgen(response)` is now `False`). Sync streaming wrappers are unchanged and still return a bare generator.
