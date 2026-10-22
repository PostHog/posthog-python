---
pypi/posthog: patch
---

The LangChain callback handler now records `$ai_http_status` as an integer for failed generations. An error that carries a string `code` and no status code (such as OpenAI's `rate_limit_exceeded`) gave a string status; it now gives `0`, as an error without a status already did.
