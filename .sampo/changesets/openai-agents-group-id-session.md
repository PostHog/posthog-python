---
pypi/posthog: minor
---

The OpenAI Agents SDK `group_id` now maps to `$ai_session_id` instead of `$ai_group_id` on `$ai_trace` and span events, so grouped runs show up as sessions in PostHog AI observability.
