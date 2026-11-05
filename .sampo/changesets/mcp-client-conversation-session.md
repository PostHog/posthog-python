---
pypi/posthog: minor
---

MCP analytics now groups tool calls into sessions by the client's own conversation id in `_meta`: ChatGPT's `openai/session`, Codex's `sessionId`, and VS Code's `vscode.conversationId`, each read only from the client its `User-Agent` or client name identifies. The client's id wins over a conversation handle and a transport session. ChatGPT gets no `conversation_id` argument and no handle, so its tool schemas and results stay unchanged. Events from `instrument()` now carry `$mcp_session_source` (`client_metadata`, `conversation`, `transport` or `generated`), so session fragmentation can be measured. `PostHogMCP.prepare_tool_list` and `prepare_tool_call` take the request's `headers` (and `client_info`), and `capture_tool_call` takes `request_meta`.
