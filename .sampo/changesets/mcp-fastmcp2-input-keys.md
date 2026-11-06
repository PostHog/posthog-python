---
pypi/posthog: patch
---

Fixed `$mcp_input_keys` recording `["[redacted]"]` on FastMCP 2.x servers that use middleware. The server's advertised schema was never read there, so every argument name looked undeclared.
