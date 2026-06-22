# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Per-server tracking state, the bounded identity LRU, and identity resolution.

Per-server state lives in a module-level ``weakref.WeakKeyDictionary`` keyed by
the server object, so state is isolated per server and garbage-collected with it
(the Python equivalent of the TS ``WeakMap``).
"""

from __future__ import annotations

import asyncio
import json
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

from .logger import log
from .sink import McpEventSink
from .types import MCPAnalyticsOptions, UserIdentity


class IdentityCache:
    """Bounded LRU of session identities, isolated per server so identities never
    bleed across server instances."""

    def __init__(self, max_size: int = 1000) -> None:
        self._cache: "OrderedDict[str, UserIdentity]" = OrderedDict()
        self._max_size = max_size

    def get(self, session_id: str) -> Optional[UserIdentity]:
        identity = self._cache.get(session_id)
        if identity is None:
            return None
        self._cache.move_to_end(session_id)
        return identity

    def set(self, session_id: str, identity: UserIdentity) -> None:
        if session_id in self._cache:
            del self._cache[session_id]
        elif len(self._cache) >= self._max_size:
            self._cache.popitem(last=False)
        self._cache[session_id] = identity

    def has(self, session_id: str) -> bool:
        return session_id in self._cache

    def size(self) -> int:
        return len(self._cache)


@dataclass
class MCPAnalyticsData:
    """All per-server tracking state."""

    options: MCPAnalyticsOptions
    sink: Optional[McpEventSink] = None
    session_id: str = ""
    session_source: str = "generated"  # "generated" | "mcp"
    last_mcp_session_id: Optional[str] = None
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    identified_sessions: IdentityCache = field(default_factory=IdentityCache)
    tool_categories: Dict[str, str] = field(default_factory=dict)
    tool_descriptions: Dict[str, str] = field(default_factory=dict)
    initialized_sessions: Set[str] = field(default_factory=set)
    server_name: Optional[str] = None
    server_version: Optional[str] = None
    session_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_server_tracking: "weakref.WeakKeyDictionary[Any, MCPAnalyticsData]" = (
    weakref.WeakKeyDictionary()
)


def get_server_tracking_data(server: Any) -> Optional[MCPAnalyticsData]:
    return _server_tracking.get(server)


def set_server_tracking_data(server: Any, data: MCPAnalyticsData) -> None:
    _server_tracking[server] = data


def are_identities_equal(a: UserIdentity, b: UserIdentity) -> bool:
    if a.distinct_id != b.distinct_id:
        return False
    if json.dumps(a.groups or {}, sort_keys=True) != json.dumps(
        b.groups or {}, sort_keys=True
    ):
        return False
    a_props = a.properties or {}
    b_props = b.properties or {}
    if set(a_props.keys()) != set(b_props.keys()):
        return False
    for key in a_props:
        if json.dumps(a_props[key], sort_keys=True, default=str) != json.dumps(
            b_props[key], sort_keys=True, default=str
        ):
            return False
    return True


def merge_identities(
    previous: Optional[UserIdentity], nxt: UserIdentity
) -> UserIdentity:
    if previous is None:
        return nxt
    return UserIdentity(
        distinct_id=nxt.distinct_id,
        properties={**(previous.properties or {}), **(nxt.properties or {})},
        groups=nxt.groups if nxt.groups is not None else previous.groups,
    )


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or asyncio.isfuture(value):
        return await value
    return value


async def handle_identify(
    data: MCPAnalyticsData,
    session_id: str,
    request: Dict[str, Any],
    extra: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve the optional ``identify`` callback, dedupe against the identity
    cache, and return an ``$identify`` event to emit only when the identity has
    materially changed (otherwise ``None``)."""
    if not data.options.identify:
        return None

    try:
        identify = data.options.identify
        if isinstance(identify, UserIdentity):
            identity_result: Optional[UserIdentity] = identify
        else:
            identity_result = await _maybe_await(identify(request, extra))

        if not identity_result:
            log(
                f"Warning: Supplied identify function returned null for session {session_id}"
            )
            return None

        previous = data.identified_sessions.get(session_id)
        merged = merge_identities(previous, identity_result)
        has_changed = not (previous and are_identities_equal(previous, merged))
        data.identified_sessions.set(session_id, merged)

        if has_changed:
            from .event_types import MCPAnalyticsEventType

            log(f"Identified session {session_id}")
            return {
                "session_id": session_id,
                "resource_name": _get_request_resource_name(request),
                "event_type": MCPAnalyticsEventType.IDENTIFY,
                "parameters": {"request": request, "extra": extra},
                "timestamp": datetime.now(timezone.utc),
            }
    except Exception as error:  # noqa: BLE001
        log(
            f"Error: identify function threw while identifying session {session_id} - {error}"
        )
    return None


async def resolve_event_properties(
    data: MCPAnalyticsData,
    request: Dict[str, Any],
    extra: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if not data.options.event_properties:
        return None
    try:
        return await _maybe_await(data.options.event_properties(request, extra)) or None
    except Exception as e:  # noqa: BLE001
        log(f"event_properties callback error: {e}")
        return None


def _get_request_resource_name(request: Any) -> str:
    if not isinstance(request, dict):
        return "Unknown"
    params = request.get("params")
    if not isinstance(params, dict):
        return "Unknown"
    name = params.get("name")
    return name if isinstance(name, str) else "Unknown"
