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

``instrument()`` requires the optional ``mcp`` dependency (``pip install posthog[mcp]``);
``PostHogMCP`` for custom dispatchers does not, so the SDK import is deferred into
``instrument()`` rather than guarded at module import.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from posthog.client import Client

from .capture import capture_event
from .constants import (
    POSTHOG_MCP_ANALYTICS_SOURCE,
    PostHogMCPAnalyticsEvent,
    PostHogMCPAnalyticsProperty,
)
from .event_types import MCPAnalyticsEventType
from .instrumentation import drain_pending
from .internal import (
    MCPAnalyticsData,
    get_server_tracking_data,
    set_server_tracking_data,
)
from .logger import log, set_logger
from .posthog_mcp import PostHogMCP
from .session import derive_session_id_from_mcp_session, new_session_id
from .sink import McpEventSink
from .tools import get_more_tools_result
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
    "get_more_tools_result",
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

    async def flush(self) -> None:
        """Await in-flight auto-captured events scheduled on the current event loop.
        Call this before ``posthog.shutdown()`` on exit so trailing tool-call events
        aren't dropped. (Then call ``posthog.flush()``/``shutdown()`` to send them.)"""
        await drain_pending()


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


def _canonical_server(server: Any) -> Any:
    """The underlying low-level server for high-level wrappers (official FastMCP and
    jlowin's fastmcp 2.0 both expose ``_mcp_server``), else the server itself. Used as
    the tracking key so instrumenting a wrapper and its underlying server resolve to
    one state instead of two divergent ones (matching the TS SDK)."""
    low_level = getattr(server, "_mcp_server", None)
    return low_level if low_level is not None else server


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

    :param server: A ``FastMCP`` server (official ``mcp.server.fastmcp`` or jlowin's
        ``fastmcp`` 2.0) or a low-level ``mcp.server.Server``.
    :param posthog_client: A posthog ``Client`` you construct and own (call
        ``shutdown()`` on exit to flush). Falls back to the global client.
    :param options: Optional :class:`MCPAnalyticsOptions`.
    """
    opts = options or MCPAnalyticsOptions()

    # The wrapping path hooks the official MCP SDK's server internals, so it needs the
    # `mcp` package — but PostHogMCP (custom dispatchers) doesn't, which is why the SDK
    # import is deferred to here instead of guarding the whole module. Raise a clear
    # error rather than letting it fall through to a silent no-op below.
    try:
        import mcp  # noqa: F401
    except ImportError:
        raise ModuleNotFoundError(
            "Please install the MCP SDK to instrument a server: 'pip install posthog[mcp]'. "
            "(PostHogMCP for custom dispatchers works without it.)"
        )
    from .compatibility import is_fastmcp, is_fastmcp_v2, is_low_level_server
    from .instrument_fastmcp import instrument_fastmcp
    from .instrument_lowlevel import instrument_fastmcp_v2, instrument_low_level

    key = _canonical_server(server)

    try:
        if opts.logger:
            set_logger(opts.logger)

        client = _resolve_client(posthog_client)
        if client is None:
            log("Warning: no PostHog client available; MCP events will not be sent.")

        if get_server_tracking_data(key) is not None:
            log("instrument() - server already instrumented, skipping initialization")
            return McpAnalytics(key)

        sink = McpEventSink(client) if client is not None else None
        data = MCPAnalyticsData(options=opts, sink=sink, session_id=new_session_id())
        set_server_tracking_data(key, data)

        if is_fastmcp(server):
            instrument_fastmcp(server, data)
        elif is_fastmcp_v2(server):
            instrument_fastmcp_v2(server, data)
        elif is_low_level_server(server):
            instrument_low_level(server, data)
        else:
            raise TypeError(
                f"Unsupported server type: {type(server)!r}. Pass a FastMCP (official or jlowin's "
                "fastmcp 2.0) or a low-level mcp.server.Server."
            )

        return McpAnalytics(key)
    except Exception as error:  # noqa: BLE001
        log(f"Warning: failed to instrument server - {error}")
        return _NoopAnalytics()
