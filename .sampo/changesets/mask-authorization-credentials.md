---
pypi/posthog: patch
---

Mask `Bearer` and `Basic` credentials in exception code variables, including values held apart from their header name, such as in header lists and ASGI scopes. Also mask signed URLs that carry a `sig` query parameter.
