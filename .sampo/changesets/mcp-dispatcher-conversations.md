---
pypi/posthog: minor
---

Add conversation and session correlation to custom `PostHogMCP` dispatchers, matching `@posthog/mcp`. `prepare_tool_list()` adds an optional `conversation_id` argument and a compatible `_mcp_instructions` output field, `prepare_tool_call()` accepts a carried `session_id` and returns the resolved `session_id` and `conversation_id`, and the new `prepare_tool_result()` delivers a minted handle without changing the original result. The capture methods accept `conversation_id`. Set `PostHogMCP(enable_conversation_id=False)` to keep the previous behavior.
