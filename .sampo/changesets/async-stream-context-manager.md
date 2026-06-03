---
pypi/posthog: patch
---

Fix async streaming responses from the AI wrappers (OpenAI, Anthropic, Gemini) so they support `async with` as well as `async for`. Previously, consuming a stream via `async with` (e.g. with pydantic-ai) raised `TypeError: 'async_generator' object does not support the asynchronous context manager protocol`.
