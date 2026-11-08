---
pypi/posthog: patch
---

The LangChain callback handler no longer loses a failed run's `$ai_generation`, `$ai_span` or `$ai_trace` event when the client is `AsyncPosthog`. It read `enable_exception_autocapture`, which `AsyncPosthog` does not have, and LangChain swallowed the error. A client without that setting now sends the failed event without an `$exception`.
