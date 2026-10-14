---
pypi/posthog: minor
---

`PostHogSpanProcessor` now adds the active PostHog context to the AI spans it exports: the distinct ID from `identify_context`, the session ID from `set_context_session` (sent as `$session_id`), and context tags as properties. Spans started inside `posthog.new_context()` reach PostHog under the right person, so one process can trace many users. Tags set by the request middleware (for example `email`, `$ip`, `$user_agent` and `$current_url`) now appear on AI events too. Attributes already on a span are kept, and other processors on the provider never see the context. Apps already using PostHog contexts (for example the Django, Flask or ASGI request middleware) will see their AI events attributed to the context's person after upgrading.
