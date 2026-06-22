# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""PostHog MCP analytics SDK — product analytics for Model Context Protocol servers.

Wrap a Python MCP server (``FastMCP`` or low-level ``mcp.server.Server``) so every
tool call, agent intent, and failure is captured to PostHog as a ``$mcp_*`` event::

    from posthog import Posthog
    from posthog.mcp import instrument
    from mcp.server.fastmcp import FastMCP

    posthog = Posthog("phc_...", host="https://us.i.posthog.com")
    server = FastMCP("my-server")
    analytics = instrument(server, posthog)

Requires the optional ``mcp`` dependency: ``pip install posthog[mcp]``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

try:
    import mcp  # noqa: F401
except ImportError:
    raise ModuleNotFoundError(
        "Please install the MCP SDK to use PostHog MCP analytics: 'pip install posthog[mcp]'"
    )

from posthog.client import Client

from .capture import capture_event
from .compatibility import is_fastmcp, is_low_level_server
from .constants import (
    POSTHOG_MCP_ANALYTICS_SOURCE,
    PostHogMCPAnalyticsEvent,
    PostHogMCPAnalyticsProperty,
)
from .event_types import MCPAnalyticsEventType
from .instrument_fastmcp import instrument_fastmcp
from .instrument_lowlevel import instrument_low_level
from .internal import (
    MCPAnalyticsData,
    get_server_tracking_data,
    set_server_tracking_data,
)
from .logger import log, set_logger
from .posthog_mcp import PostHogMCP
from .session import derive_session_id_from_mcp_session, new_session_id
from .sink import McpEventSink
from .types import (
    CaptureEventData,
    MCPAnalyticsContextOptions,
    MCPAnalyticsOptions,
    PreparedToolCall,
    UserIdentity,
)
from .version import __version__

__all__ = [
    "instrument",
    "McpAnalytics",
    "PostHogMCP",
    "MCPAnalyticsOptions",
    "MCPAnalyticsContextOptions",
    "UserIdentity",
    "CaptureEventData",
    "PreparedToolCall",
    "derive_session_id_from_mcp_session",
    "set_logger",
    "POSTHOG_MCP_ANALYTICS_SOURCE",
    "PostHogMCPAnalyticsEvent",
    "PostHogMCPAnalyticsProperty",
    "__version__",
]


class McpAnalytics:
    """Handle returned by :func:`instrument`. Use it to capture custom events for
    the instrumented server without passing the server object around."""

    def __init__(self, key: Any) -> None:
        self._key = key

    async def capture(self, event: str, properties: Optional[dict] = None) -> None:
        """Capture a custom event for this server. ``event`` is sent verbatim (a
        customer-defined event, so it is not ``$``-prefixed)."""
        if not isinstance(event, str) or not event:
            raise ValueError(
                'capture() requires an event name, e.g. await analytics.capture("feedback_submitted")'
            )
        data = get_server_tracking_data(self._key)
        if data is None:
            return
        coro = capture_event(
            data,
            {
                "session_id": data.session_id,
                "event_type": MCPAnalyticsEventType.CUSTOM,
                "event_name": event,
                "timestamp": datetime.now(timezone.utc),
                "properties": properties,
            },
        )
        if coro is not None:
            await coro


class _NoopAnalytics(McpAnalytics):
    def __init__(self) -> None:  # noqa: D401 - graceful degradation handle
        super().__init__(None)

    async def capture(self, event: str, properties: Optional[dict] = None) -> None:
        return None


def _resolve_client(posthog_client: Optional[Client]) -> Optional[Client]:
    if posthog_client is not None:
        return posthog_client
    try:
        from posthog import setup

        return setup()
    except Exception:  # noqa: BLE001
        return None


def instrument(
    server: Any,
    posthog_client: Optional[Client] = None,
    options: Optional[MCPAnalyticsOptions] = None,
) -> McpAnalytics:
    """Instrument an MCP server so PostHog auto-captures tool calls, tool listings,
    initialize, identity, and exceptions. Returns a handle whose ``capture()``
    records custom events.

    Idempotent per server instance — a second call reuses the existing tracking
    state instead of double-wrapping. Degrades to a no-op handle on any failure so
    the host application keeps working.

    :param server: A ``FastMCP`` server (low-level ``Server`` support: see M3).
    :param posthog_client: A posthog ``Client`` you construct and own (call
        ``shutdown()`` on exit to flush). Falls back to the global client.
    :param options: Optional :class:`MCPAnalyticsOptions`.
    """
    opts = options or MCPAnalyticsOptions()
    try:
        if opts.logger:
            set_logger(opts.logger)

        client = _resolve_client(posthog_client)
        if client is None:
            log("Warning: no PostHog client available; MCP events will not be sent.")

        if get_server_tracking_data(server) is not None:
            log("instrument() - server already instrumented, skipping initialization")
            return McpAnalytics(server)

        sink = McpEventSink(client) if client is not None else None
        data = MCPAnalyticsData(options=opts, sink=sink, session_id=new_session_id())
        set_server_tracking_data(server, data)

        if is_fastmcp(server):
            instrument_fastmcp(server, data)
        elif is_low_level_server(server):
            instrument_low_level(server, data)
        else:
            raise TypeError(
                f"Unsupported server type: {type(server)!r}. Pass a FastMCP or low-level mcp.server.Server."
            )

        return McpAnalytics(server)
    except Exception as error:  # noqa: BLE001
        log(f"Warning: failed to instrument server - {error}")
        return _NoopAnalytics()
