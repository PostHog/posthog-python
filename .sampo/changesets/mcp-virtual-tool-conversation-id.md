---
pypi/posthog: patch
---

Anchor MCP analytics virtual tools to the conversation. With `enable_conversation_id=True`, `get_more_tools` and `send_feedback` now advertise `conversation_id`, echo a minted handle back, and stamp `$mcp_conversation_id` — whatever you rename them to.

They were exempt before, so a `$mcp_feedback` or `$mcp_missing_capability` event was filed under a `$session_id` of its own: an agent's complaint about a tool landed in a different session than the call it was complaining about, along with a spurious second `$mcp_initialize`. Reports now share the session they are about.

The virtual tools still never get the injected `context` argument — they state their intent through their own.
