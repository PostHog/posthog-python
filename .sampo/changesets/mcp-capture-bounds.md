---
pypi/posthog: patch
---

MCP analytics: bound capture work and close feedback leaks. `$mcp_intent` and the `send_feedback` fields are redacted only as far as truncation can keep, so a megabyte-long intent no longer stalls capture for seconds. Captured tool arguments are limited to 100 entries and depth 10 before they are sanitized, credential-named arguments are captured as `[redacted]` without being read, and deeply nested arguments no longer raise. Object `$mcp_feedback_<name>` extras redact identifiers in keys and numbers.
