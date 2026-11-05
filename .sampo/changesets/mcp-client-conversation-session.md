---
pypi/posthog: minor
---

MCP analytics now groups stateless ChatGPT calls by the conversation id ChatGPT sends in each request's `_meta["openai/session"]`. A call with no conversation handle and no transport session gets its `$session_id` from it, mints no handle, and gets no prompt-back block, so a server with `enable_conversation_id=False` keeps its tool schemas and results unchanged and still groups ChatGPT traffic. `PostHogMCP.prepare_tool_call` applies the same rule to its `request_meta` argument. Events now carry `$mcp_session_source` (`conversation`, `transport`, `client_metadata` or `generated`), so session fragmentation can be measured.
