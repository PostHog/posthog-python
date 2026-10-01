---
pypi/posthog: patch
---

MCP analytics sanitizes only the part of a large string that truncation keeps, so capturing a multi-megabyte tool result no longer blocks the server for seconds. The captured event is unchanged.
