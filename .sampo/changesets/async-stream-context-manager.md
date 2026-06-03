---
pypi/posthog: patch
---

Fix `TypeError: 'async_generator' object does not support the asynchronous context manager protocol` when using the async AI wrappers (`posthog.ai.openai.AsyncOpenAI`, `posthog.ai.anthropic.AsyncAnthropic`) with libraries such as pydantic-ai that consume streaming responses via `async with`. Streaming responses now support both `async for` and `async with`, and exiting the context closes the underlying provider stream.
