"""Resolve model argument ownership on low-level servers without a tool registry."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

from ._internal import MCPAnalyticsData
from ._model_parameters import can_inject_model_parameter, is_capture_model_enabled
from .logger import log


async def resolve_model_ownership(
    data: MCPAnalyticsData,
    name: str,
    list_page: Callable[[Optional[str]], Awaitable[Any]],
) -> bool:
    if not is_capture_model_enabled(data.options.capture_model):
        return False
    if name in data.tool_model_parameter_injected:
        return data.tool_model_parameter_injected[name]
    try:
        return await asyncio.wait_for(
            _find_model_ownership(name, list_page), timeout=0.25
        )
    except Exception:  # noqa: BLE001 - discovery must not prevent tool dispatch
        log(
            "Warning: Could not resolve model argument ownership; leaving tool arguments unchanged."
        )
        return False


async def _find_model_ownership(
    name: str, list_page: Callable[[Optional[str]], Awaitable[Any]]
) -> bool:
    cursor = None
    seen = set()
    for _ in range(16):
        response = await list_page(cursor)
        result = getattr(response, "root", response)
        ownership = _model_ownership(result, name)
        if ownership is not None:
            return ownership
        cursor = _next_cursor(result)
        if cursor is None or cursor in seen:
            return False
        seen.add(cursor)
    return False


def _model_ownership(result: Any, name: str) -> Optional[bool]:
    for tool in getattr(result, "tools", []):
        if getattr(tool, "name", None) == name:
            schema = getattr(tool, "input_schema", None)
            if schema is None:
                schema = getattr(tool, "inputSchema", None)
            return can_inject_model_parameter(schema)
    return None


def _next_cursor(result: Any) -> Optional[str]:
    if hasattr(result, "next_cursor"):
        cursor = result.next_cursor
    else:
        cursor = getattr(result, "nextCursor", None)
    if not isinstance(cursor, str):
        return None
    return cursor or None
