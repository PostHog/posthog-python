---
pypi/posthog: minor
---

The Gemini adapter now covers two surfaces of `genai.Client` it previously lacked. `Client.aio.models` reaches the tracked async models adapter, so `await client.aio.models.generate_content(...)` works without swapping the class out for `AsyncClient`, and `client.files` (plus `client.aio.files`, and `AsyncClient.files` for the async Files API) passes through to the provider, so multimodal flows that upload a file before referencing it in `contents` no longer fail. Every surface of one client shares a single provider client instead of opening its own.
