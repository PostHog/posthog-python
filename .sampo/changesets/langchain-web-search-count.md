---
pypi/posthog: patch
---

The LangChain callback handler now records `$ai_web_search_count` for Anthropic responses that report `server_tool_use.web_search_requests`, as the OpenAI, Anthropic and Gemini wrappers and `posthog-js` already do.
