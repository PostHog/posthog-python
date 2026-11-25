---
pypi/posthog: minor
---

`posthog.mcp`: record `input_required` rounds as `$mcp_input_required`, not as tool calls. On the 2026-07-28 revision, a tool that asks for more input, such as a confirmation form, sent a `$mcp_tool_call` for the round and another for the retry, so one confirmed call counted twice. `instrument()` now sends `$mcp_input_required` with `$mcp_tool_name`, `$mcp_duration_ms`, and `$mcp_input_request_methods`, and only the retry that completes the call is a `$mcp_tool_call`. A round returned to a client on an earlier revision, which cannot receive it, is a failed `$mcp_tool_call` with `$mcp_error_type: "input_required"`. Custom dispatchers record rounds with the new `PostHogMCP.capture_input_required()`.
