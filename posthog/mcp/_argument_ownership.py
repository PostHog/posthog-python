"""Resolve ownership of arguments that PostHog adds to MCP tool schemas."""

from __future__ import annotations

import copy
import inspect
from collections.abc import Mapping
from typing import Any, FrozenSet, Optional

from ._context_parameters import is_context_enabled, schema_has_param
from ._model_parameters import is_capture_model_enabled
from .logger import log

_COMPLEX_SCHEMA_KEYS = ("$ref", "oneOf", "allOf", "anyOf")
_OWNERSHIP_MARKER = "__posthog_mcp_argument_ownership__"


def analytics_owned_parameters(
    options: Any,
    input_schema: Any,
    *,
    previously_owned: FrozenSet[str] = frozenset(),
) -> FrozenSet[str]:
    """Return the enabled arguments that PostHog can add to this schema."""
    enabled = set()
    if is_context_enabled(options.context):
        enabled.add("context")
    if options.enable_conversation_id:
        enabled.add("conversation_id")
    if is_capture_model_enabled(options.capture_model):
        enabled.add("llm_model")

    if isinstance(input_schema, dict) and any(
        input_schema.get(key) for key in _COMPLEX_SCHEMA_KEYS
    ):
        return frozenset()
    return frozenset(
        name
        for name in enabled
        if name in previously_owned or not schema_has_param(input_schema, name)
    )


def cache_listed_tool_ownership(
    data: Any, tool: Any, *, schema_attribute: str
) -> FrozenSet[str]:
    """Cache ownership from the host schema before PostHog changes it."""
    name = getattr(tool, "name", None)
    if not isinstance(name, str):
        return frozenset()
    schema = getattr(tool, schema_attribute, None)
    previously_owned = _marked_parameter_ownership(tool, schema)
    ownership = analytics_owned_parameters(
        data.options, schema, previously_owned=previously_owned
    )
    data.tool_analytics_parameter_ownership[name] = ownership
    if "llm_model" in previously_owned:
        data.tool_model_parameter_injected[name] = True
    return ownership


def mark_listed_tool_ownership(
    tool: Any, ownership: FrozenSet[str], *, schema_attribute: str
) -> None:
    """Mark injected fields without adding data to the serialized descriptor."""
    schema = getattr(tool, schema_attribute, None)
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return

    existing = getattr(tool, _OWNERSHIP_MARKER, None)
    marked = dict(existing) if isinstance(existing, Mapping) else {}
    for name in ownership:
        if name not in properties:
            continue
        try:
            marked[name] = copy.deepcopy(properties[name])
        except Exception:  # noqa: BLE001 - ownership tracking must not break listing
            marked[name] = properties[name]
    try:
        setattr(tool, _OWNERSHIP_MARKER, marked)
    except Exception:  # noqa: BLE001 - some tool models may reject private fields
        return


def _marked_parameter_ownership(tool: Any, schema: Any) -> FrozenSet[str]:
    """Read fields that this SDK added to a reused tool descriptor."""
    marked = getattr(tool, _OWNERSHIP_MARKER, None)
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(marked, Mapping) or not isinstance(properties, dict):
        return frozenset()
    return frozenset(
        name
        for name, declaration in marked.items()
        if isinstance(name, str) and properties.get(name) == declaration
    )


async def resolve_lowlevel_tool_ownership(
    data: Any, name: str
) -> Optional[FrozenSet[str]]:
    """Resolve one raw tool. A served listing on this instance has priority."""
    if name in data.tool_analytics_parameter_ownership:
        return data.tool_analytics_parameter_ownership[name]

    resolver = data.options.resolve_original_tool
    if resolver is None:
        return None

    try:
        descriptor = resolver(name)
        if inspect.isawaitable(descriptor):
            descriptor = await descriptor
    except Exception as error:  # noqa: BLE001 - analytics must not break dispatch
        log(f"Warning: resolve_original_tool failed for tool {name!r}: {error}")
        return None

    if descriptor is None:
        return None
    schema = _descriptor_input_schema(descriptor)
    return analytics_owned_parameters(data.options, schema)


def _descriptor_input_schema(descriptor: Any) -> Any:
    """Read MCP 1.x, MCP 2.x, and dictionary tool descriptors."""
    if isinstance(descriptor, Mapping):
        if "inputSchema" in descriptor:
            return descriptor["inputSchema"]
        return descriptor.get("input_schema")

    schema = getattr(descriptor, "inputSchema", None)
    if schema is not None:
        return schema
    return getattr(descriptor, "input_schema", None)
