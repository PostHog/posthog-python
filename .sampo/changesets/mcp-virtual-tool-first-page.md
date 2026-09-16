---
pypi/posthog: patch
---

Fix MCP analytics virtual tool injection on a paginated `tools/list`. `get_more_tools` was added to every page and `send_feedback` only to the last; both now go on the first page, matching `@posthog/mcp`.

- A real tool of yours named `get_more_tools` is no longer silently swallowed. It wins, and the warning names `missing_capability_tool_name`.
- Custom tool names are honoured consistently: a renamed `get_more_tools` no longer gets a stray `conversation_id` argument, and a real `get_more_tools` of yours keeps its `context` injection.
- A server that returns the same `tools/list` result object on every request no longer reads PostHog's own injected tool back as a name collision.
- Collision warnings now reach the `posthog.mcp` logger too, so they are visible without setting the `logger` option.

On a paginated listing, the first page's `$mcp_tools_list.listed_tool_names` now carries the virtual tools even when a next page follows.
