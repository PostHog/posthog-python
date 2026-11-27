---
posthog: patch
---

Tool spans that report an error now always carry `$ai_error`: in privacy mode it is the fixed label `ToolError`, and outside it media is redacted and empty content gives the same label. Claude Agent SDK tool inputs are redacted when the span is sent, so privacy and media capture settings changed while a tool ran apply. Closing the `query()` stream early now sends tool spans still waiting for a result, and LangChain `PostHogMiddleware` tool spans set `$ai_span_type: "tool"`.
