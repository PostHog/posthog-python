---
pypi/posthog: patch
---

On FastMCP 2.x servers with middleware, `posthog.mcp` no longer tries to read the advertised tool schema on each call. FastMCP 2.x can only list tools through request middleware, which can spend rate limits, so the lookup failed and logged an error on every call. Argument names there stay redacted in `$mcp_input_keys`, as before.
