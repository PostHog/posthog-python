---
pypi/posthog: minor
---

AI integrations take capture options. Every wrapper method that takes `posthog_properties` also takes `posthog_options` (OpenAI, Anthropic, Gemini, sync and async, and the Claude Agent SDK `query()` and `PostHogClaudeSDKClient`), and the Gemini clients take it as a default for every call. LangChain's `CallbackHandler` and `PostHogMiddleware`, and `instrument()` and the tracing processors for OpenAI Agents and the Claude Agent SDK, take `options=`. LangChain also reads `posthog_options` from run metadata. These options win over context options and `super_options`. An event without a distinct ID still always sends `process_person_profile: False`, and a value that is not a dict is logged and ignored.
