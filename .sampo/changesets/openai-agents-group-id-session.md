---
pypi/posthog: minor
---

The OpenAI Agents SDK `group_id` now also maps to `$ai_session_id` on `$ai_trace` and span events, so grouped runs show up as sessions in PostHog AI observability. `$ai_group_id` is still emitted alongside it.
