---
pypi/posthog: minor
---

feat(mcp): emit `$mcp_protocol_version` on MCP analytics events — the MCP spec version, recovered from the session token across stateless pods (parity with the TypeScript SDK). `PostHogMCP` capture methods gain a `protocol_version` argument.
