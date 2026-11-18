---
pypi/posthog: patch
---

Spans that `PostHogSpanProcessor` and `PostHogTraceExporter` redact, in privacy mode or to mask remote MCP credentials, now keep the counts of attributes, events and links the SDK dropped under its span limits. The redacted copy previously reported 0, as the context copy did before #1266.
