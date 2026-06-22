# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Inject a required ``context`` parameter into a tool's JSON Schema so agents
state their intent. Operates on the already-serialized JSON Schema dict (the
``mcp`` SDK exposes tool ``inputSchema`` as a plain dict)."""

from __future__ import annotations

import copy
from typing import Any, Dict, Optional, Union

from .constants import DEFAULT_CONTEXT_PARAMETER_DESCRIPTION
from .logger import log
from .types import MCPAnalyticsContextOptions


def is_context_enabled(context: Union[bool, MCPAnalyticsContextOptions, None]) -> bool:
    return context is not False


def get_context_description(
    context: Union[bool, MCPAnalyticsContextOptions, None],
) -> Optional[str]:
    if isinstance(context, MCPAnalyticsContextOptions):
        return context.description
    return None


def add_context_parameter_to_schema(
    input_schema: Optional[Dict[str, Any]],
    tool_name: str = "unknown",
    description_override: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return a new JSON Schema dict with a required ``context`` string property
    added. Returns the input unchanged (logging a warning) for schemas that
    already define ``context`` or use ``oneOf``/``allOf``/``anyOf``."""
    schema = input_schema

    if (
        schema
        and isinstance(schema.get("properties"), dict)
        and "context" in schema["properties"]
    ):
        log(
            f"WARN: Tool \"{tool_name}\" already has 'context' parameter. Skipping context injection."
        )
        return schema

    if schema and (schema.get("oneOf") or schema.get("allOf") or schema.get("anyOf")):
        log(
            f'WARN: Tool "{tool_name}" has complex schema (oneOf/allOf/anyOf). Skipping context injection.'
        )
        return schema

    if not schema:
        schema = {"type": "object", "properties": {}, "required": []}

    # Deep copy to avoid mutating the tool's stored schema.
    schema = copy.deepcopy(schema)

    if not isinstance(schema.get("properties"), dict):
        schema["properties"] = {}

    # additionalProperties: false would reject the injected context — remove it
    # (the SDK adds this when converting Pydantic models to JSON Schema).
    if schema.get("additionalProperties") is False:
        schema.pop("additionalProperties", None)

    schema["properties"]["context"] = {
        "type": "string",
        "description": description_override or DEFAULT_CONTEXT_PARAMETER_DESCRIPTION,
    }

    required = schema.get("required")
    if isinstance(required, list):
        if "context" not in required:
            required.append("context")
    else:
        schema["required"] = ["context"]

    return schema
