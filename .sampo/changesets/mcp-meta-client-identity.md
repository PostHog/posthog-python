---
pypi/posthog: minor
---

feat(mcp): capture client identity under the MCP 2026-07-28 revision, which removes the `initialize` handshake and carries the client name/version and protocol version in every request's `_meta` instead. `$mcp_client_name`, `$mcp_client_version`, and `$mcp_protocol_version` keep populating on both `mcp` 1.x (where the SDK ignores `_meta`, so we read it ourselves) and `mcp>=2` (where the SDK renamed the seams we read identity from). Legacy clients are unaffected. Also fixes tool-error detection on `mcp>=2`, where `CallToolResult.isError` is spelled `is_error` — without it every v2 tool error was recorded as a success. On `mcp>=2`, `tools/list` is not yet captured: that release replaces the `request_handlers` dispatch the listing seam hooks.
