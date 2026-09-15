---
pypi/posthog: minor
---

Enable MCP model capture and conversation correlation by default. Advertised tool schemas gain an `llm_model` argument (never enforced at dispatch) and eligible tool results gain a conversation handle; `MCPAnalyticsOptions(capture_model=False, enable_conversation_id=False)` restores the previous shape. Fresh low-level instances now read the self-reported model instead of staying silent.
