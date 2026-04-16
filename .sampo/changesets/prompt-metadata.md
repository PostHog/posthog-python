---
pypi/posthog: minor
---

`Prompts.get()` now accepts `with_metadata=True` and returns a `PromptResult` dataclass containing `source` (`api`, `cache`, `stale_cache`, or `code_fallback`), `name`, and `version` alongside the prompt text. The previous plain-string return is deprecated and will be removed in a future major version.
