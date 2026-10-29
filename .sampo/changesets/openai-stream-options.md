---
pypi/posthog: patch
---

Fix streaming OpenAI chat completions when `stream_options` is `None` or `NOT_GIVEN` (it raised a `TypeError`), and stop adding `include_usage` to the `stream_options` dict the caller passed in.
