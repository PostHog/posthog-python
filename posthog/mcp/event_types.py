# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Internal SDK event vocabulary.

These values are the protocol-shaped event types this SDK observes before
mapping them to PostHog event names (see ``posthog_events.py``). They are never
sent to PostHog directly.
"""


class MCPAnalyticsEventType:
    """Protocol-shaped event types observed by the SDK (internal dispatch keys)."""

    IDENTIFY = "posthog:identify"
    CUSTOM = "posthog:custom"
    MCP_MISSING_CAPABILITY = "mcp:missing_capability"
    MCP_INITIALIZE = "mcp:initialize"
    MCP_PROMPTS_GET = "mcp:prompts/get"
    MCP_PROMPTS_LIST = "mcp:prompts/list"
    MCP_RESOURCES_LIST = "mcp:resources/list"
    MCP_RESOURCES_READ = "mcp:resources/read"
    MCP_TOOLS_CALL = "mcp:tools/call"
    MCP_TOOLS_LIST = "mcp:tools/list"
