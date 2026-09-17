---
pypi/posthog: patch
---

MCP analytics now adds its virtual tools (`get_more_tools`, `send_feedback`) to the first `tools/list` page only, rather than to every page and to the last page respectively.

If one of your own tools already uses a virtual tool's name, PostHog warns and names the option that renames its own — `missing_capability_tool_name`, or `collect_feedback`'s `tool_name`. Warnings also reach the `posthog.mcp` logger, so you see them without setting the `logger` option.
