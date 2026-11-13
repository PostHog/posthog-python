---
pypi/posthog: minor
---

`posthog.mcp`: a failed tool call now sets `$mcp_error_status` to the HTTP status behind it, read from the raised exception or its cause chain (`status_code`, `statusCode` or `status`, on the exception or its `response`, as `httpx.HTTPStatusError` and `requests.HTTPError` carry it). Failures from an upstream API now group by status in MCP Analytics instead of landing in "unknown".
