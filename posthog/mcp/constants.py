# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Public event names and property wire-keys for PostHog MCP analytics.

These are plain classes with string class attributes (not ``enum.StrEnum``,
which is 3.11+) so they work on the repo's minimum Python 3.10 and read the same
way as the TypeScript const objects: ``PostHogMCPAnalyticsEvent.TOOL_CALL``.
"""

INACTIVITY_TIMEOUT_IN_MINUTES = 30

DEFAULT_CONTEXT_PARAMETER_DESCRIPTION = (
    "Explain why you are calling this tool and how it fits into the user's overall goal. "
    "This parameter is used for analytics and user intent tracking. YOU MUST provide 15-25 "
    "words (count carefully). NEVER use first person ('I', 'we', 'you') - maintain "
    "third-person perspective. NEVER include sensitive information such as credentials, "
    "passwords, or personal data. Example (20 words): \"Searching across the organization's "
    "repositories to find all open issues related to performance complaints and latency "
    'issues for team prioritization."'
)

DEFAULT_CONVERSATION_ID_DESCRIPTION = (
    "Echo the conversation_id from the server's previous response. The server provides it on "
    "the first call — never invent one, and do not issue parallel tool calls until you have it."
)

POSTHOG_MCP_ANALYTICS_SOURCE = "posthog_mcp_analytics"


class PostHogMCPAnalyticsEvent:
    """PostHog-owned event names. All ``$``-prefixed per the PostHog convention;
    non-``$`` names would be treated as customer-defined events."""

    CUSTOM = "$mcp_custom"
    EXCEPTION = "$exception"
    IDENTIFY = "$identify"
    INITIALIZE = "$mcp_initialize"
    MISSING_CAPABILITY = "$mcp_missing_capability"
    PROMPT_GET = "$mcp_prompt_get"
    PROMPTS_LIST = "$mcp_prompts_list"
    RESOURCE_READ = "$mcp_resource_read"
    RESOURCES_LIST = "$mcp_resources_list"
    TOOL_CALL = "$mcp_tool_call"
    TOOLS_LIST = "$mcp_tools_list"


class PostHogMCPAnalyticsProperty:
    """PostHog property wire-keys emitted on MCP events."""

    CLIENT_NAME = "$mcp_client_name"
    CLIENT_VERSION = "$mcp_client_version"
    CONVERSATION_ID = "$mcp_conversation_id"
    DURATION_MS = "$mcp_duration_ms"
    IS_ERROR = "$mcp_is_error"
    INTENT = "$mcp_intent"
    INTENT_SOURCE = "$mcp_intent_source"
    LISTED_TOOL_NAMES = "$mcp_listed_tool_names"
    PARAMETERS = "$mcp_parameters"
    RESOURCE_NAME = "$mcp_resource_name"
    RESPONSE = "$mcp_response"
    SERVER_NAME = "$mcp_server_name"
    SERVER_VERSION = "$mcp_server_version"
    SESSION_ID = "$session_id"
    SOURCE = "$mcp_source"
    TOOL_CATEGORY = "$mcp_tool_category"
    TOOL_DESCRIPTION = "$mcp_tool_description"
    TOOL_NAME = "$mcp_tool_name"
