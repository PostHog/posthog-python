---
pypi/posthog: minor
---

Add conversation and session correlation to custom `PostHogMCP` dispatchers, matching `@posthog/mcp`. `prepare_tool_list()` adds an optional `conversation_id` field to each compatible tool input schema and a compatible `_mcp_instructions` output field. `prepare_tool_call()` accepts a carried `session_id`. The new `prepare_tool_result()` delivers a minted handle without changing the original result. Tool and report capture methods accept `conversation_id`. Existing dispatchers must call `prepare_tool_result()` to deliver new handles. Set `PostHogMCP(enable_conversation_id=False)` to keep the previous behavior.
