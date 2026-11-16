---
pypi/posthog: patch
---

An Anthropic error that arrives as an `error` event inside a stream, such as `overloaded_error`, is no longer recorded with `$ai_http_status: 200`. The anthropic SDK raises it with the stream's 200 status, so `$ai_http_status` now comes from the error type (529 for `overloaded_error`, 429 for `rate_limit_error`), or is 0 for an unknown type. This applies to the LangChain callback handler and to every capture that reads the status through `_get_http_status`.
