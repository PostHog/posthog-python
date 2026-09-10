# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Second delivery channel for the conversation handle: ``structuredContent``.

Two halves that must stay in this order:

  1. declare ``_mcp_instructions`` on the tool's advertised output schema
     (at ``tools/list``)
  2. write it into the result's ``structuredContent`` (on every response)

Needed because clients that read ``structuredContent`` — which they do whenever
a tool declares an output schema — never see the ``content`` text block that
carries the handle. Measured against Claude Code, the echo rate was 100% for
schema-less tools and 0% for schema-declaring ones before this mirror existed
(posthog-js ADR-0004).

The declaration is what makes the write safe: MCP clients validate
``structuredContent`` against the schema from ``tools/list``, and generated
schemas commonly carry ``additionalProperties: false``, so an undeclared key is
not ignored — it fails the entire tool result. Only tools that got the
declaration are ever written to, and an instance that never served a listing
fails closed.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Optional, Tuple

from .logger import log

MCP_INSTRUCTIONS_KEY = "_mcp_instructions"

_INSTRUCTIONS_FIELD_DESCRIPTION = "Server-issued metadata for this conversation."
_CONVERSATION_ID_FIELD_DESCRIPTION = "The server-issued conversation identifier."

# `outputSchema` on MCP SDK 1.x models, `output_schema` on 2.x (same wire field).
_OUTPUT_SCHEMA_ATTRS = ("outputSchema", "output_schema")
_STRUCTURED_CONTENT_ATTRS = ("structuredContent", "structured_content")


def _read_attr(obj: Any, names: Tuple[str, ...]) -> Tuple[Optional[str], Any]:
    """The first of ``names`` this object actually carries, and its value."""
    for name in names:
        if hasattr(obj, name):
            return name, getattr(obj, name)
    return None, None


def _is_our_declaration(declaration: Any) -> bool:
    """Whether an existing :data:`MCP_INSTRUCTIONS_KEY` property is one we wrote
    on a previous listing, told apart from a customer's by our description."""
    return (
        isinstance(declaration, dict)
        and declaration.get("description") == _INSTRUCTIONS_FIELD_DESCRIPTION
    )


def can_declare_output_instructions(output_schema: Any) -> bool:
    """True when :data:`MCP_INSTRUCTIONS_KEY` can safely be declared on this
    tool's advertised output schema.

    Requires a plain-object JSON Schema we can extend. A tool with no output
    schema has nothing to mirror into and keeps working through ``content``; a
    composed schema (``oneOf``/``allOf``/``anyOf``/``$ref``) has no single
    ``properties`` bag to add to. Both stay content-only, matching the policy on
    the input side.
    """
    if not isinstance(output_schema, dict):
        return False
    if (
        output_schema.get("$ref")
        or output_schema.get("oneOf")
        or output_schema.get("allOf")
        or output_schema.get("anyOf")
    ):
        return False
    properties = output_schema.get("properties")
    # A malformed `properties` is harmless until we try to declare into it, so
    # refuse rather than raise inside the tools/list wrapper and fail the listing.
    if properties is not None and not isinstance(properties, dict):
        return False
    return not properties or MCP_INSTRUCTIONS_KEY not in properties


def add_instructions_to_output_schema(tool: Any) -> bool:
    """Declare an optional :data:`MCP_INSTRUCTIONS_KEY` on ``tool``'s output
    schema, in place. Returns whether the declaration was made — the caller
    records that answer as ownership, and only declared tools are ever mirrored
    into.

    The property is never added to ``required``: a result without it must stay
    valid, since every tool result predating this change lacks it.
    """
    attr, original = _read_attr(tool, _OUTPUT_SCHEMA_ATTRS)
    if attr is None or not original:
        # No output schema means the client reads `content`, where the handle
        # already rides. Nothing to declare, and nothing broken.
        return False

    name = getattr(tool, "name", "unknown")
    if not can_declare_output_instructions(original):
        properties = original.get("properties") if isinstance(original, dict) else None
        if isinstance(properties, dict) and MCP_INSTRUCTIONS_KEY in properties:
            # Our own declaration from an earlier listing: servers that hand back
            # persistent Tool objects (a module-level list, or two servers sharing
            # tools) hit this on every re-list. Report it as declared — reading it
            # as customer-owned would silently switch the mirror off for exactly
            # the clients it exists for, and blame the customer in the log.
            if _is_our_declaration(properties[MCP_INSTRUCTIONS_KEY]):
                return True
            log(
                f"WARN: Tool \"{name}\" already declares '{MCP_INSTRUCTIONS_KEY}' in its "
                "output schema. Leaving it alone."
            )
        else:
            log(
                f'WARN: Tool "{name}" has a complex output schema (oneOf/allOf/anyOf/$ref). '
                f"Skipping '{MCP_INSTRUCTIONS_KEY}' declaration; its session handle stays "
                "content-only."
            )
        return False

    # Deep copy: the server may reuse or freeze the schema object it handed us.
    schema = copy.deepcopy(original)
    if not isinstance(schema.get("properties"), dict):
        schema["properties"] = {}
    schema["properties"][MCP_INSTRUCTIONS_KEY] = {
        "type": "object",
        "description": _INSTRUCTIONS_FIELD_DESCRIPTION,
        "properties": {
            "conversation_id": {
                "type": "string",
                "description": _CONVERSATION_ID_FIELD_DESCRIPTION,
            }
        },
    }
    try:
        setattr(tool, attr, schema)
    except Exception:  # noqa: BLE001 - some schema attrs may be read-only
        log(f"WARN: could not set {attr} on tool {name}")
        return False
    return True


def build_conversation_instructions(conversation_id: str) -> Dict[str, Any]:
    """The payload mirrored into ``structuredContent``."""
    return {"conversation_id": conversation_id}


def mirror_instructions_into_structured_content(
    result: Any, conversation_id: str
) -> Tuple[Any, bool]:
    """Write :data:`MCP_INSTRUCTIONS_KEY` into a result's ``structuredContent``.

    Returns ``(result, delivered)``. Unlike the text block this rides *every*
    response rather than only the one that minted the handle, so an agent that
    dropped it can read it back.

    Handles every shape a tool result takes across the adapters: the
    ``(content, structured)`` tuple from FastMCP 1.x's ``convert_result`` path,
    a ``CallToolResult`` model (``structuredContent`` on 1.x,
    ``structured_content`` on 2.x), and a plain dict. Leaves the result untouched
    when there is no plain-object structured content to extend, or when the tool
    already produced its own key — customer data wins.
    """
    payload = build_conversation_instructions(conversation_id)

    # FastMCP 1.x convert_result path: (content_list, structured)
    if isinstance(result, tuple) and len(result) == 2:
        structured = result[1]
        if not isinstance(structured, dict) or MCP_INSTRUCTIONS_KEY in structured:
            return result, False
        return (result[0], {**structured, MCP_INSTRUCTIONS_KEY: payload}), True

    if isinstance(result, dict):
        for key in _STRUCTURED_CONTENT_ATTRS:
            structured = result.get(key)
            if isinstance(structured, dict) and MCP_INSTRUCTIONS_KEY not in structured:
                return {
                    **result,
                    key: {**structured, MCP_INSTRUCTIONS_KEY: payload},
                }, True
        return result, False

    # CallToolResult model (or the ServerResult wrapper around one).
    target = getattr(result, "root", result)
    attr, structured = _read_attr(target, _STRUCTURED_CONTENT_ATTRS)
    if (
        attr is None
        or not isinstance(structured, dict)
        or MCP_INSTRUCTIONS_KEY in structured
    ):
        return result, False
    updated = {**structured, MCP_INSTRUCTIONS_KEY: payload}
    # Copy rather than mutate: a tool is free to return a shared or cached
    # result object, and pinning one conversation's handle onto it would serve
    # that handle to every later caller (and, through the handle, collapse
    # unrelated clients into one session).
    copy_model = getattr(target, "model_copy", None)
    if callable(copy_model):
        try:
            new_target = copy_model(update={attr: updated})
        except Exception:  # noqa: BLE001 - never let delivery break the tool path
            return result, False
        if target is result:
            return new_target, True
        rewrap = getattr(result, "model_copy", None)
        if callable(rewrap):
            try:
                return rewrap(update={"root": new_target}), True
            except Exception:  # noqa: BLE001
                return result, False
        return result, False
    try:
        setattr(target, attr, updated)
    except Exception:  # noqa: BLE001 - never let delivery break the tool path
        return result, False
    return result, True
