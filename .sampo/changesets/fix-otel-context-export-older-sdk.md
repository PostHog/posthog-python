---
pypi/posthog: patch
---

`PostHogSpanProcessor` no longer breaks export of spans started inside a PostHog context on opentelemetry-sdk 1.20 and 1.21. The span copy that carries the context's distinct ID, session ID and tags held plain dicts and tuples, but those SDK versions' OTLP encoder reads `.dropped` from its attributes, events and links and failed with `AttributeError`. The copy now also keeps the counts of attributes, events and links the SDK dropped under its span limits, which it previously reported as 0.
