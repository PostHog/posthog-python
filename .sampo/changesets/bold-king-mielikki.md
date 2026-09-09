---
pypi/posthog: minor
---

Capture MCP model identifiers from client metadata or an SDK-owned self-report field.
Model capture remains opt-in and preserves application-owned fields across repeated tool listings.

MCP context and conversation-ID injection now preserve `additionalProperties: false` in tool schemas, including when model capture is disabled. Servers that validate these schemas now reject undeclared arguments that earlier SDK versions allowed. Declared analytics fields remain valid.
