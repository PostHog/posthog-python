---
posthog: patch
---

Use the context's distinct ID for streamed LLM analytics generations when no `posthog_distinct_id` is passed, as non-streamed calls already do.
