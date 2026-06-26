# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Session id resolution: prefer a transport-supplied MCP session id (derived
deterministically so it survives restarts) over an SDK-generated one, which
rolls over after an inactivity timeout."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .constants import INACTIVITY_TIMEOUT_IN_MINUTES
from ._ids import deterministic_prefixed_id, new_prefixed_id
from ._internal import MCPAnalyticsData

__all__ = ["derive_session_id_from_mcp_session"]


def new_session_id() -> str:
    return new_prefixed_id("ses")


def derive_session_id_from_mcp_session(mcp_session_id: str) -> str:
    """Deterministic SDK session id for an MCP protocol session, so the same MCP
    session correlates to one ``$session_id`` across server restarts."""
    return deterministic_prefixed_id("ses", mcp_session_id)


async def resolve_session_id(
    data: MCPAnalyticsData, mcp_session_id: Optional[str]
) -> str:
    """Resolve the session id for a request. Mutates per-server state under a lock
    so concurrent async requests can't race on session rotation."""
    async with data.session_lock:
        now = datetime.now(timezone.utc)

        if mcp_session_id:
            data.session_id = derive_session_id_from_mcp_session(mcp_session_id)
            data.last_mcp_session_id = mcp_session_id
            data.session_source = "mcp"
            data.last_activity = now
            return data.session_id

        # Once a session is MCP-derived, keep it even if a later request arrives
        # without the MCP session id, so the session doesn't fragment.
        if data.session_source == "mcp" and data.last_mcp_session_id:
            data.last_activity = now
            return data.session_id

        timeout_seconds = INACTIVITY_TIMEOUT_IN_MINUTES * 60
        if (now - data.last_activity).total_seconds() > timeout_seconds:
            data.session_id = new_session_id()
            data.session_source = "generated"
        data.last_activity = now
        return data.session_id
