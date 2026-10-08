"""Resolve ownership of arguments that PostHog adds to MCP tool schemas."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any, Dict, FrozenSet, Optional, Tuple

from ._context_parameters import is_context_enabled, schema_has_param
from ._model_parameters import is_capture_model_enabled
from .logger import log

_COMPLEX_SCHEMA_KEYS = ("$ref", "oneOf", "allOf", "anyOf")


def analytics_owned_parameters(
    options: Any,
    input_schema: Any,
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
        name for name in enabled if not schema_has_param(input_schema, name)
    )


def cache_listed_tool_ownership(
    data: Any, tool: Any, *, schema_attribute: str
) -> FrozenSet[str]:
    """Cache ownership from the host schema before PostHog changes it."""
    name = getattr(tool, "name", None)
    if not isinstance(name, str):
        return frozenset()
    schema = getattr(tool, schema_attribute, None)
    ownership = analytics_owned_parameters(data.options, schema)
    data.tool_analytics_parameter_ownership[name] = ownership
    if isinstance(schema, dict):
        data.tool_input_schemas[name] = schema
    else:
        data.tool_input_schemas.pop(name, None)
    return ownership


async def resolve_lowlevel_tool_ownership(
    data: Any, name: str
) -> Tuple[Optional[FrozenSet[str]], Optional[Dict[str, Any]]]:
    """Resolve one raw tool. A served listing on this instance has priority."""
    if name in data.tool_analytics_parameter_ownership:
        return (
            data.tool_analytics_parameter_ownership[name],
            data.tool_input_schemas.get(name),
        )

    resolver = data.options.resolve_original_tool
    if resolver is None:
        return None, None

    try:
        descriptor = resolver(name)
        if inspect.isawaitable(descriptor):
            descriptor = await descriptor
        if descriptor is None:
            return None, None
        schema = _descriptor_input_schema(descriptor)
        if not isinstance(schema, dict):
            log(
                f"Warning: resolve_original_tool failed for tool {name!r}: "
                "resolver returned no usable input schema"
            )
            return None, None
        return analytics_owned_parameters(data.options, schema), schema
    except Exception as error:  # noqa: BLE001 - analytics must not break dispatch
        log(f"Warning: resolve_original_tool failed for tool {name!r}: {error}")
        return None, None


def _descriptor_input_schema(descriptor: Any) -> Any:
    """Read MCP 1.x, MCP 2.x, and dictionary tool descriptors."""
    if isinstance(descriptor, Mapping):
        if "inputSchema" in descriptor:
            return descriptor["inputSchema"]
        return descriptor.get("input_schema")

    schema = getattr(descriptor, "input_schema", None)
    if schema is not None:
        return schema
    return getattr(descriptor, "inputSchema", None)
