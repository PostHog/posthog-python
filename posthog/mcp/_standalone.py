"""Argument ownership for jlowin's standalone ``fastmcp.FastMCP`` (2.0+), shared by
the MCP SDK v1 and v2 adapters. It keeps a live registry, so ownership is read
from the tool's advertised schema per request instead of a prior listing."""

from __future__ import annotations

from typing import Any, FrozenSet, Optional

from ._context_parameters import is_context_enabled, schema_has_param
from ._internal import MCPAnalyticsData
from ._model_parameters import can_inject_model_parameter, is_capture_model_enabled
from .logger import log


async def standalone_injected_parameters(
    server: Any, data: MCPAnalyticsData, name: str, version: Optional[str]
) -> Optional[FrozenSet[str]]:
    """The analytics arguments the SDK injected into this tool's schema, which
    are the only ones safe to strip before FastMCP validates the call. Mirrors
    the listing-time injection rules: nothing is injected into a composed
    schema, and ``llm_model`` follows ``can_inject_model_parameter``.

    Listings from other requests can have different application-owned
    parameters (middleware, versions), so this resolves in the current request.
    ``None`` means the schema could not be read; callers then strip nothing.
    """
    try:
        from fastmcp.utilities.versions import VersionSpec, version_sort_key

        version_spec = VersionSpec(eq=version) if version else None
        # Middleware can shadow registered tools, so resolve the effective listing.
        candidates = [
            tool
            for tool in await server.list_tools()
            if tool.name == name
            and (version_spec is None or version_spec.matches(tool.version))
        ]
        tool = max(candidates, key=version_sort_key, default=None)
        if tool is None:
            tool = await server.get_tool(name, version=version_spec)
        schema = getattr(tool, "parameters", None)
    except Exception as error:  # noqa: BLE001 - schema lookup must not prevent dispatch
        log(f"PostHog MCP: could not resolve schema for tool {name!r} - {error}")
        return None
    if not isinstance(schema, dict):
        return None
    injected = set()
    if not any(schema.get(key) for key in ("oneOf", "allOf", "anyOf")):
        if is_context_enabled(data.options.context):
            injected.add("context")
        if data.options.enable_conversation_id:
            injected.add("conversation_id")
    if is_capture_model_enabled(data.options.capture_model) and (
        can_inject_model_parameter(schema)
    ):
        injected.add("llm_model")
    return frozenset(key for key in injected if not schema_has_param(schema, key))
