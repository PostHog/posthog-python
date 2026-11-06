---
pypi/posthog: minor
---

MCP analytics tool definitions change in three ways:

- `get_more_tools` and `send_feedback` declare `readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint` as `false`, because a call records data in your PostHog project. Their annotations no longer carry a `title`.
- The injected `context` argument has a shorter description.
- The injected `llm_model` argument is always optional.

These changes alter your published tool definitions. If your server is listed in the ChatGPT or Claude directories, those platforms may re-review the updated tools.
