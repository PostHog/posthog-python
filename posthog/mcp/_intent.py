# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Resolve ``$mcp_intent`` from the agent-supplied ``context`` argument (source
``context_parameter``) or the customer's ``intent_fallback`` callback (source
``inferred``)."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, Tuple

from ._context_parameters import is_context_enabled
from ._internal import MCPAnalyticsData, _maybe_await
from .logger import log

# (intent, source)
ResolvedIntent = Tuple[str, str]


def _get_context_argument(request: Dict[str, Any]) -> Optional[str]:
    params = request.get("params") or {}
    arguments = params.get("arguments") or {}
    context = arguments.get("context")
    if isinstance(context, str) and context.strip():
        return context
    return None


def _normalize_intent(intent: Any) -> Optional[str]:
    if not isinstance(intent, str):
        return None
    trimmed = intent.strip()
    return trimmed or None


async def _run_intent_fallback(
    data: MCPAnalyticsData, request: Dict[str, Any], extra: Optional[Dict[str, Any]]
) -> Optional[ResolvedIntent]:
    if not data.options.intent_fallback:
        return None
    try:
        result = data.options.intent_fallback(request, extra)
        if asyncio.iscoroutine(result):
            result = await _maybe_await(result)
        intent = _normalize_intent(result)
        return (intent, "inferred") if intent else None
    except Exception as error:  # noqa: BLE001
        log(f"intent_fallback callback error: {error}")
        return None


async def resolve_tool_call_intent(
    data: MCPAnalyticsData,
    request: Dict[str, Any],
    extra: Optional[Dict[str, Any]] = None,
) -> Optional[ResolvedIntent]:
    from .tools import resolve_missing_capability_tool_name

    context_argument = _get_context_argument(request)
    name = (request.get("params") or {}).get("name")
    missing_name = resolve_missing_capability_tool_name(data.options)
    if (
        is_context_enabled(data.options.context)
        and name != missing_name
        and context_argument
    ):
        return (context_argument, "context_parameter")
    return await _run_intent_fallback(data, request, extra)


def set_event_intent(event: Dict[str, Any], resolved: Optional[ResolvedIntent]) -> None:
    if not resolved:
        return
    event["user_intent"], event["user_intent_source"] = resolved
