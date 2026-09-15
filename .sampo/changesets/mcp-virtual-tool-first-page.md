---
pypi/posthog: patch
---

Fix MCP analytics virtual-tool injection on a paginated `tools/list`. `get_more_tools` was appended to every page, so a cursor-following client saw it once per page, and `send_feedback` was appended to the last page only, hiding it from every client that never follows `nextCursor`. Both now go on the first page, matching `@posthog/mcp`. An empty-string cursor is treated as a continuation page, not the first one.

`get_more_tools` gains the name-collision handling `send_feedback` already had: a real tool using the name wins and is dispatched normally instead of being silently swallowed, and the warning names `missing_capability_tool_name` as the way to keep both. Ownership is now also checked at call time on every adapter, which covers a call reaching a process that never served a listing. Configuring both virtual tools with the same name is detected and warned about. Collision warnings go to the `posthog.mcp` standard-library logger as well as the `logger` option, so a default-configured host sees them. `PostHogMCP.prepare_tool_call` honours `original_tool` for the missing-capability tool as it already did for feedback.

Also fixes two cases where the missing-capability tool's name was matched literally rather than as configured: a renamed virtual tool no longer gets a `conversation_id` argument the default-named one never got, and a real tool of yours named `get_more_tools` keeps its normal `context` injection and has its value captured as `$mcp_intent`.

Note for anyone charting advertised tools: on a paginated listing the first page's `$mcp_tools_list.listed_tool_names` now carries the virtual tools even when a next page follows, and continuation pages no longer carry `send_feedback`.
