---
pypi/posthog: minor
---

Align ASGI, Flask, and Django tracing-header capture with peer SDKs. Add capture_tracing_headers and POSTHOG_MW_CAPTURE_TRACING_HEADERS, enabled by default once an integration is installed. Keep the trust-named options as deprecated aliases, honor explicit opt-outs, and prefer the capture-named option when both are configured. Share header sanitization and request-context attribution across integrations; explicit capture identity and session properties retain precedence.
