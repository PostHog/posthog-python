---
pypi/posthog: minor
---

Enable MCP model capture and conversation correlation by default. Advertised tool schemas gain an `llm_model` argument (never enforced at dispatch) and eligible tool results gain a conversation handle; `MCPAnalyticsOptions(capture_model=False, enable_conversation_id=False)` restores the previous shape. Fresh low-level instances now read the self-reported model instead of staying silent.

Standalone FastMCP on MCP SDK 1.x skips `llm_model` injection when application middleware can change tool listing or dispatch. Model metadata capture remains enabled; this prevents cold replicas from rejecting injected arguments and preserves replacement tools' own arguments.
