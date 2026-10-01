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
        if _is_our_declaration(schema["properties"][CONVERSATION_ID_PARAM_NAME]):
            return schema
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
    schema["properties"][CONVERSATION_ID_PARAM_NAME] = {
        "type": "string",
        "description": DEFAULT_CONVERSATION_ID_DESCRIPTION,
    }
    return schema


def can_inject_conversation_id(input_schema: Any) -> bool:
    """Whether the SDK can own ``conversation_id`` on this input schema. An
    application-declared field or a composed schema stays the application's,
    so its value is never read as a handle or stripped before dispatch."""
    if not isinstance(input_schema, dict):
        return True
    properties = input_schema.get("properties")
    if isinstance(properties, dict) and CONVERSATION_ID_PARAM_NAME in properties:
        return _is_our_declaration(properties[CONVERSATION_ID_PARAM_NAME])
    return not any(input_schema.get(key) for key in ("$ref", "oneOf", "allOf", "anyOf"))


def _is_our_declaration(declaration: Any) -> bool:
    return (
        isinstance(declaration, dict)
        and declaration.get("type") == "string"
        and declaration.get("description") == DEFAULT_CONVERSATION_ID_DESCRIPTION
    )


def extract_conversation_id(args: Any) -> Optional[str]:
    if not isinstance(args, dict):
        return None
    value = args.get(CONVERSATION_ID_PARAM_NAME)
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def resolve_conversation_id(enabled: bool, args: Any) -> Tuple[Optional[str], bool]:
    """Return the conversation id and whether the SDK minted it.

    Lowercased on the way in: the shape test is case-insensitive but the hash
    behind ``$session_id`` is not, so an uppercased echo (some hosts normalise
    uuids) would land in a different session than the call that minted it."""
    if not enabled:
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
    """Append a handle block to a result copy when it has content."""
    block: Any = build_prompt_back(conversation_id)
    if isinstance(result, dict):
        if not isinstance(result.get("content"), list):
            return result
        return {**result, "content": [*result["content"], block]}
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], list):
        return ([*result[0], block], result[1])
    if isinstance(result, list):
        return [*result, block]

    target = getattr(result, "root", result)
    content = getattr(target, "content", None)
    if not isinstance(content, list):
        return result
    try:
        import mcp.types as mcp_types  # noqa: PLC0415

        block = mcp_types.TextContent(type="text", text=block["text"])
    except ImportError:
        pass

    copy_model = getattr(target, "model_copy", None)
    if callable(copy_model):
        try:
            updated = copy_model(update={"content": [*content, block]})
        except Exception:  # noqa: BLE001 - delivery must not break a tool call
            return result
    else:
        updated = _copy_with_attr(target, "content", [*content, block])
        if updated is None:
            return result
    if target is result:
        return updated

    rewrap = getattr(result, "model_copy", None)
    if callable(rewrap):
        try:
            return rewrap(update={"root": updated})
        except Exception:  # noqa: BLE001 - delivery must not break a tool call
            return result
    wrapped = _copy_with_attr(result, "root", updated)
    return result if wrapped is None else wrapped


def _copy_with_attr(value: Any, attr: str, updated: Any) -> Optional[Any]:
    try:
        copied = copy.copy(value)
    except Exception:  # noqa: BLE001 - delivery must not break a tool call
        return None
    if copied is value:
        return None
    try:
        setattr(copied, attr, updated)
    except Exception:  # noqa: BLE001 - read-only objects fail closed
        return None
    return copied
