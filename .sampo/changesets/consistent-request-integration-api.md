---
pypi/posthog: minor
---

Align ASGI, Flask, and Django request integration configuration. ASGI and Flask now default to inheriting the effective client's exception-autocapture setting, with explicit capture overrides and context-only operation. Flask and Django require opt-in to accept PostHog tracing identity/session headers. Django adds property-named settings and extraction methods while preserving legacy aliases and its historical capture default; setting POSTHOG_MW_CAPTURE_EXCEPTIONS to None enables client inheritance. Document client routing, request-filter semantics, and event enrichment independently of error tracking.
