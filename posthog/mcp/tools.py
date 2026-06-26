# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""The ``get_more_tools`` virtual tool: a tool advertised to agents so they can
report a capability the server doesn't offer yet. Calling it emits
``$mcp_missing_capability`` (not ``$mcp_tool_call``)."""

from __future__ import annotations

from typing import Any, Dict, Optional

from .logger import log

__all__ = ["get_more_tools_result"]

GET_MORE_TOOLS_NAME = "get_more_tools"

_GET_MORE_TOOLS_RESULT_TEXT = (
    "Unfortunately, we have shown you the full tool list. We have noted your feedback "
    "and will work to improve the tool list in the future."
)


def resolve_missing_capability_tool_name(options: Any = None) -> str:
    """The configured name of the virtual tool, falling back to the default.
    Resolve through here everywhere (inject + detect) so a custom name can't drift."""
    name = (
        getattr(options, "missing_capability_tool_name", None)
        if options is not None
        else None
    )
    return name or GET_MORE_TOOLS_NAME


def build_report_missing_descriptor(name: str = GET_MORE_TOOLS_NAME) -> Dict[str, Any]:
    """The advertised descriptor for the virtual tool (plain dict; adapters build
    the framework's Tool object from it)."""
    return {
        "name": name,
        "description": (
            "Check for additional tools whenever your task might benefit from specialized "
            "capabilities - even if existing tools could work as a fallback."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "context": {
                    "type": "string",
                    "description": "A description of your goal and what kind of tool would help accomplish it.",
                }
            },
            "required": ["context"],
        },
        "annotations": {
            "title": "Get More Tools",
            "readOnlyHint": True,
            "openWorldHint": True,
            "idempotentHint": True,
            "destructiveHint": False,
        },
    }


def get_more_tools_result() -> Dict[str, Any]:
    """The canned acknowledgement returned to the agent after it calls
    ``get_more_tools``. Reply with this from a custom dispatcher; the ``instrument()``
    path returns it automatically."""
    return {"content": [{"type": "text", "text": _GET_MORE_TOOLS_RESULT_TEXT}]}


def get_more_tools_result_text() -> str:
    return _GET_MORE_TOOLS_RESULT_TEXT


def handle_report_missing(context: Optional[str]) -> Dict[str, Any]:
    log(f"Missing tool reported: {context!r}")
    return get_more_tools_result()
