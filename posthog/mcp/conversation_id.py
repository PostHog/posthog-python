# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Optional ``conversation_id`` loop-back. When enabled, the SDK injects a
``conversation_id`` parameter into every tool, mints one when the agent doesn't
supply it, appends a prompt-back asking the agent to echo it on later calls, and
captures it as ``$mcp_conversation_id`` — stitching calls across reconnects."""

from __future__ import annotations

import copy
from typing import Any, Dict, Optional, Tuple

from .constants import DEFAULT_CONVERSATION_ID_DESCRIPTION
from .ids import _uuid7
from .logger import log

CONVERSATION_ID_PARAM_NAME = "conversation_id"


def add_conversation_id_to_schema(
    input_schema: Optional[Dict[str, Any]], tool_name: str = "unknown"
) -> Optional[Dict[str, Any]]:
    """Return a new JSON Schema with an optional ``conversation_id`` string property.
    Skips schemas that already define it or use ``oneOf``/``allOf``/``anyOf``."""
    schema = input_schema
    if (
        schema
        and isinstance(schema.get("properties"), dict)
        and CONVERSATION_ID_PARAM_NAME in schema["properties"]
    ):
        log(
            f"WARN: Tool \"{tool_name}\" already has '{CONVERSATION_ID_PARAM_NAME}'. Skipping injection."
        )
        return schema
    if schema and (schema.get("oneOf") or schema.get("allOf") or schema.get("anyOf")):
        log(
            f'WARN: Tool "{tool_name}" has complex schema. Skipping conversation_id injection.'
        )
        return schema

    if not schema:
        schema = {"type": "object", "properties": {}, "required": []}
    schema = copy.deepcopy(schema)
    if not isinstance(schema.get("properties"), dict):
        schema["properties"] = {}
    if schema.get("additionalProperties") is False:
        schema.pop("additionalProperties", None)
    schema["properties"][CONVERSATION_ID_PARAM_NAME] = {
        "type": "string",
        "description": DEFAULT_CONVERSATION_ID_DESCRIPTION,
    }
    return schema


def extract_conversation_id(args: Any) -> Optional[str]:
    if not isinstance(args, dict):
        return None
    value = args.get(CONVERSATION_ID_PARAM_NAME)
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def resolve_conversation_id(
    enabled: bool,
    args: Any,
    tool_name: Optional[str],
    missing_capability_tool_name: str,
) -> Tuple[Optional[str], bool]:
    """Return ``(conversation_id, minted)``. Disabled or get_more_tools → ``(None, False)``;
    agent supplied → ``(value, False)``; agent omitted → ``(new uuid, True)``."""
    if not enabled or tool_name == missing_capability_tool_name:
        return None, False
    supplied = extract_conversation_id(args)
    if supplied:
        return supplied, False
    return _uuid7(), True


def can_inject_prompt_back(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("isError") is True:
        return False
    return isinstance(result.get("content"), list)


def build_prompt_back(conversation_id: str) -> Dict[str, Any]:
    return {
        "type": "text",
        "text": (
            f"[SERVER]: Reuse conversation_id={conversation_id} on every subsequent tool call in this "
            "conversation. Required for the server to correlate calls and provide context-aware results."
        ),
    }


def inject_prompt_back(result: Any, conversation_id: str) -> Any:
    if not can_inject_prompt_back(result):
        return result
    return {
        **result,
        "content": [*result["content"], build_prompt_back(conversation_id)],
    }
