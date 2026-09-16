---
pypi/posthog: patch
---

Fix MCP analytics virtual tool injection on a paginated `tools/list`. `get_more_tools` was added to every page and `send_feedback` only to the last; both now go on the first page, matching `@posthog/mcp`.

- A real tool of yours sharing a virtual tool's name now wins instead of being silently swallowed, and the warning names the option that renames PostHog's — `missing_capability_tool_name` or `collect_feedback`'s `tool_name`. Warnings also reach the `posthog.mcp` logger, so you see them without setting the `logger` option.
- If PostHog cannot tell whether a name is yours or its own, the call is delegated to your server rather than intercepted.
- A server that returns the same `tools/list` result object on every request no longer has PostHog's injected tool read back as a collision, which stopped both tools being advertised from the second listing on.

On a paginated listing, the first page's `$mcp_tools_list.listed_tool_names` now carries the virtual tools even when a next page follows.
