# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Public event names and property wire-keys for PostHog MCP analytics.

These are plain classes with string class attributes (not ``enum.StrEnum``,
which is 3.11+) so they work on the repo's minimum Python 3.10 and read the same
way as the TypeScript const objects: ``PostHogMCPAnalyticsEvent.TOOL_CALL``.
"""

__all__ = [
    "POSTHOG_MCP_ANALYTICS_SOURCE",
    "PostHogMCPAnalyticsEvent",
    "PostHogMCPAnalyticsProperty",
]

INACTIVITY_TIMEOUT_IN_MINUTES = 30

DEFAULT_CONTEXT_PARAMETER_DESCRIPTION = (
    "A few words on why you are calling this tool, for the server developer's "
    'analytics. Describe people and things by role, such as "a customer". Do not '
    "quote the user or include names, emails, or other personal details."
)

DEFAULT_CONVERSATION_ID_DESCRIPTION = (
    "The conversation_id this server returned earlier in this conversation, unchanged, including "
    "after later user messages. Omit it if you have none. Recorded for the server developer's "
    "analytics."
)

DEFAULT_MODEL_PARAMETER_DESCRIPTION = (
    'The model identifier you are running as, for example "claude-opus-4-8" or "gpt-5.2". '
    'Pass "unknown" if you are not certain. Recorded for the server developer\'s analytics.'
)

POSTHOG_MCP_ANALYTICS_SOURCE = "posthog_mcp_analytics"
POSTHOG_MCP_LIB_NAME = "posthog-python-mcp"


class PostHogMCPAnalyticsEvent:
    """PostHog-owned event names. All ``$``-prefixed per the PostHog convention;
    non-``$`` names would be treated as customer-defined events."""

    CUSTOM = "$mcp_custom"
    EXCEPTION = "$exception"
    FEEDBACK = "$mcp_feedback"
    # Deprecated: the SDK no longer sends `$identify`.
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
    CLIENT_USER_AGENT = "$mcp_client_user_agent"
    CLIENT_VERSION = "$mcp_client_version"
    VENDOR_CLIENT = "$mcp_vendor_client"
    PROTOCOL_VERSION = "$mcp_protocol_version"
    CONVERSATION_ID = "$mcp_conversation_id"
    DURATION_MS = "$mcp_duration_ms"
    ERROR_MESSAGE = "$mcp_error_message"
    ERROR_TYPE = "$mcp_error_type"
    FEEDBACK_DETAILS = "$mcp_feedback_details"
    FEEDBACK_FRICTION_POINTS = "$mcp_feedback_friction_points"
    FEEDBACK_SENTIMENT = "$mcp_feedback_sentiment"
    FEEDBACK_SUGGESTED_IMPROVEMENT = "$mcp_feedback_suggested_improvement"
    FEEDBACK_SUMMARY = "$mcp_feedback_summary"
    FEEDBACK_TASK_COMPLETED = "$mcp_feedback_task_completed"
    FEEDBACK_TOOL = "$mcp_feedback_tool"
    FEEDBACK_TYPE = "$mcp_feedback_type"
    INTERFACE = "$mcp_interface"
    IS_ERROR = "$mcp_is_error"
    INTENT = "$mcp_intent"
    INTENT_SOURCE = "$mcp_intent_source"
    INPUT_ALIASES_USED = "$mcp_input_aliases_used"
    INPUT_KEYS = "$mcp_input_keys"
    LLM_MODEL = "$mcp_llm_model"
    LLM_MODEL_SOURCE = "$mcp_llm_model_source"
    LISTED_TOOL_NAMES = "$mcp_listed_tool_names"
    PARAMETERS = "$mcp_parameters"
    RESOURCE_NAME = "$mcp_resource_name"
    RESPONSE = "$mcp_response"
    SERVER_NAME = "$mcp_server_name"
    SERVER_BUILD = "$mcp_server_build"
    SERVER_VERSION = "$mcp_server_version"
    SESSION_ID = "$session_id"
    SOURCE = "$mcp_source"
    TOOL_CATEGORY = "$mcp_tool_category"
    TOOL_DESCRIPTION = "$mcp_tool_description"
    TOOL_NAME = "$mcp_tool_name"
