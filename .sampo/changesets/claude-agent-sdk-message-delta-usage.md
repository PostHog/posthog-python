---
pypi/posthog: patch
---

The Claude Agent SDK integration now takes the cumulative input and cache token counts from the final `message_delta` usage, which grow past the `message_start` values when server tools such as web search run, and sets `$ai_web_search_count` from `server_tool_use.web_search_requests`. Streamed generations used to under-report input tokens and cache writes and never recorded the web search count.
