---
pypi/posthog: patch
---

`posthog.ai.anthropic`: keep server tool blocks (`server_tool_use`, `web_search_tool_result`, and other block types the wrapper doesn't format) in `$ai_output_choices` for streamed responses, with the server tool's streamed input. Non-streamed responses already recorded these blocks, so a streamed web search generation showed only the final text and lost the query and results.
