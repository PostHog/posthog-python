# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Optional ``conversation_id`` loop-back. When enabled, the SDK injects a
``conversation_id`` parameter into every tool, mints one when the agent doesn't
supply it, appends a prompt-back asking the agent to echo it on later calls, and
captures it as ``$mcp_conversation_id`` — stitching calls across reconnects."""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, Optional, Tuple

from .constants import DEFAULT_CONVERSATION_ID_DESCRIPTION
from ._ids import _uuid7
from .logger import log

CONVERSATION_ID_PARAM_NAME = "conversation_id"

# The shape of every id we mint: a uuidv7. Used to tell an echo of our own
# handle from a value the agent made up. The shape check matters because the
# handle becomes ``$session_id`` — without it, two unrelated users both sending
# "conv-1" would share a session (byte-parity with posthog-js's
# MINTED_CONVERSATION_ID).
_MINTED_CONVERSATION_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


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
    agent echoed a handle we could have minted → ``(value, False)``; anything
    else (omitted, or a value the agent made up) → ``(new uuid, True)``.

    Lowercased on the way in: the shape test is case-insensitive but the hash
    behind ``$session_id`` is not, so an uppercased echo (some hosts normalise
    uuids) would land in a different session than the call that minted it."""
    if not enabled or tool_name == missing_capability_tool_name:
        return None, False
    supplied = extract_conversation_id(args)
    if supplied and _MINTED_CONVERSATION_ID.match(supplied):
        return supplied.lower(), False
    return _uuid7(), True


def can_inject_prompt_back(result: Any) -> bool:
    """Whether the prompt-back can ride this result's ``content`` — the only
    requirement is a list to append to. Errored results included on purpose: a
    tool that fails on the first call of a conversation is exactly when the
    agent needs the handle, or the retry starts a fresh conversation and the
    failure and its fix land in different sessions."""
    if not isinstance(result, dict):
        return False
    return isinstance(result.get("content"), list)


def build_prompt_back(conversation_id: str) -> Dict[str, Any]:
    """The content block carrying the handle back to the agent.

    Plain data, not an instruction. Tool results are untrusted content, so a
    server sentence telling the model what to do on every later call is exactly
    the shape a client's prompt-injection filter looks for — and a stripped
    block means the handle never arrives and conversation sessions quietly stop
    working. It also renders in the user's transcript. Same payload as
    ``@posthog/mcp``.
    """
    return {
        "type": "text",
        "text": json.dumps({"conversation_id": conversation_id}),
    }


def inject_prompt_back(result: Any, conversation_id: str) -> Any:
    if not can_inject_prompt_back(result):
        return result
    return {
        **result,
        "content": [*result["content"], build_prompt_back(conversation_id)],
    }
