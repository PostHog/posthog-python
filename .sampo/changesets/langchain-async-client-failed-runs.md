---
pypi/posthog: patch
---

`AsyncPosthog` no longer breaks two exception paths that read `enable_exception_autocapture`, a setting `AsyncPosthog` does not have:

- The LangChain callback handler no longer loses a failed run's `$ai_generation`, `$ai_span` or `$ai_trace` event. LangChain swallowed the error, so the event was never sent. The failed event is now sent without an `$exception`.
- `new_context(client=<AsyncPosthog>)` no longer raises `AttributeError`. Without `capture_exceptions`, it now leaves exceptions uncaptured; pass `capture_exceptions=True` to capture them.
