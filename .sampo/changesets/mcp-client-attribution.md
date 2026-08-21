---
posthog: minor
---

feat(mcp): capture `$mcp_client_user_agent` and `$mcp_vendor_client` so MCP usage can be attributed to a product surface. `clientInfo.name` only says which client *library* is calling — Anthropic reports `claude-code` from the CLI, the Agent SDK, the VS Code extension and the desktop app alike — so `$mcp_client_name` collapses every surface into one bucket and the harness breakdown reads 100% "Other" for Python-backed servers. The distinguishing detail lives in the User-Agent parenthetical (`claude-code/2.1.0 (cli)` vs `(sdk-ts)`) and in vendor headers like `x-anthropic-client`. Both are captured raw and classified at query time, so labels can improve without an SDK release. HTTP transports only: stdio and in-memory servers carry no headers and their events are unchanged. Custom dispatchers pass their own via new `client_user_agent` / `vendor_client` arguments on every `PostHogMCP.capture_*` method. Parity with `@posthog/mcp`.
