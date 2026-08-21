# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""PostHog MCP analytics SDK — product analytics for Model Context Protocol servers.

Wrap a Python MCP server so every tool call, agent intent, and failure is
captured to PostHog as a ``$mcp_*`` event. Works with the MCP Python SDK 1.x
*and* 2.x (the 2026-07-28 spec revision) — the high-level server class moved
between majors, but ``instrument()`` is the same::

    from posthog import Posthog
    from posthog.mcp import instrument

    # MCP SDK 2.x (spec 2026-07-28)
    from mcp.server.mcpserver import MCPServer
    server = MCPServer("my-server")

    # MCP SDK 1.x
    # from mcp.server.fastmcp import FastMCP
    # server = FastMCP("my-server")

    posthog = Posthog("phc_...", host="https://us.i.posthog.com")
    analytics = instrument(server, posthog)

Install is just ``pip install posthog``. ``instrument()`` needs the MCP SDK at runtime,
but anyone wrapping a server already has it (you built the server with it), so it's
treated as a peer dependency — imported lazily and version-checked inside ``instrument()``
rather than bundled. ``PostHogMCP`` for custom dispatchers needs nothing beyond posthog.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from posthog.client import Client

from ._capture import capture_event
from .constants import (
    POSTHOG_MCP_ANALYTICS_SOURCE,
    PostHogMCPAnalyticsEvent,
    PostHogMCPAnalyticsProperty,
)
from ._event_types import MCPAnalyticsEventType
from ._instrumentation import drain_pending
from ._internal import (
    MCPAnalyticsData,
    get_server_tracking_data,
    set_server_tracking_data,
)
from .logger import log, set_logger
from .posthog_mcp import PostHogMCP
from .request_headers import get_request_headers
from .session import (
    derive_session_id_from_conversation,
    derive_session_id_from_mcp_session,
    new_session_id,
)
from .session_token import (
    MCP_SESSION_HEADER,
    SessionTokenPayload,
    decode_session_id,
    encode_session_id,
)
from .asgi import (
    PostHogMcpStatelessSessionMiddleware,
    autowire_stateless_mint,
    get_mcp_session,
)
from ._sink import McpEventSink
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
    # Read HTTP headers inside identify / intent_fallback /
    # event_properties callbacks on either SDK major: the per-request context
    # arrives as extra["ctx"] and its shape differs between them.
    "get_request_headers",
    "derive_session_id_from_mcp_session",
    # Conversation-anchored sessions: the cross-SDK derivation contract with
    # posthog-js (the 2026-07-28 revision has no protocol sessions, so the
    # agent-echoed conversation_id is the only cross-pod session carrier).
    "derive_session_id_from_conversation",
    # Self-encoded session tokens for stateless / multi-pod servers. Minted onto
    # the `Mcp-Session-Id` response header by PostHogMcpStatelessSessionMiddleware
    # and decoded on every request; codec is exported for custom HTTP layers.
    "PostHogMcpStatelessSessionMiddleware",
    "get_mcp_session",
    "encode_session_id",
    "decode_session_id",
    "SessionTokenPayload",
    "MCP_SESSION_HEADER",
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
        """Await this server's in-flight auto-captures on the current event loop.
        Call this before ``posthog.shutdown()`` on exit so trailing tool-call events
        aren't dropped. (Then call ``posthog.flush()``/``shutdown()`` to send them.)"""
        data = get_server_tracking_data(self._key)
        if data is not None:
            await drain_pending(data)


class _NoopAnalytics(McpAnalytics):
    def __init__(self) -> None:  # noqa: D401 - graceful degradation handle
        super().__init__(None)

    async def capture(self, event: str, properties: Optional[dict] = None) -> None:
        return None

    async def flush(self) -> None:
        # There is no tracking key to look up or pending work to drain.
        return None


def _resolve_client(posthog_client: Optional[Client]) -> Optional[Client]:
    if posthog_client is not None:
        return posthog_client
    try:
        from posthog import setup

        return setup()
    except Exception:  # noqa: BLE001
        return None


def _warn_if_unsupported_mcp_version() -> None:
    """The adapters hook private MCP SDK seams (``_tool_manager``, ``_mcp_server``
    / ``_lowlevel_server``, the request-handler registries) tested against
    ``mcp>=1.26,<3`` — both the 1.x line and the 2.x line (spec 2026-07-28).
    Since ``mcp`` is a peer dependency we don't pin, advise at runtime when the
    installed version is outside that range rather than failing hard (older/newer
    may still mostly work)."""
    try:
        from importlib.metadata import version

        installed = version("mcp")
        major, minor = (int(p) for p in installed.split(".")[:2])
    except Exception:  # noqa: BLE001 - never let a version probe break instrument()
        return
    if (major, minor) < (1, 26) or major >= 3:
        log(
            f"Warning: PostHog MCP analytics is tested against mcp>=1.26,<3; found {installed}. "
            "Instrumentation hooks private SDK internals and may behave unexpectedly."
        )


def _canonical_server(server: Any) -> Any:
    """The underlying low-level server for high-level wrappers (SDK 1.x FastMCP and
    jlowin's fastmcp expose ``_mcp_server``; SDK 2.x MCPServer renamed it
    ``_lowlevel_server``), else the server itself. Used as the tracking key so
    instrumenting a wrapper and its underlying server resolve to one state instead
    of two divergent ones (matching the TS SDK)."""
    low_level = getattr(server, "_mcp_server", None) or getattr(
        server, "_lowlevel_server", None
    )
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

    :param server: A high-level server — SDK 1.x ``mcp.server.fastmcp.FastMCP``,
        SDK 2.x ``mcp.server.mcpserver.MCPServer``, or jlowin's ``fastmcp.FastMCP``
        — or a low-level ``mcp.server.lowlevel.Server`` (either SDK major).
    :param posthog_client: A posthog ``Client`` you construct and own (call
        ``shutdown()`` on exit to flush). Falls back to the global client.
    :param options: Optional :class:`MCPAnalyticsOptions`.
    """
    opts = options or MCPAnalyticsOptions()

    # Install the logger first so the version advisory below (and any warning) is
    # actually visible rather than going to the default no-op sink.
    if opts.logger:
        set_logger(opts.logger)

    # The wrapping path hooks the official MCP SDK's server internals, so it needs the
    # `mcp` package. It's a peer dependency (you already have it — you built the server
    # with it), imported lazily here rather than bundled. PostHogMCP (custom dispatchers)
    # doesn't need it at all. Raise a clear error rather than a silent no-op below.
    try:
        import mcp  # noqa: F401
    except ImportError:
        raise ModuleNotFoundError(
            "instrument() needs the MCP SDK. Install it with: pip install 'mcp>=1.26'. "
            "(PostHogMCP for custom dispatchers works without it.)"
        )
    _warn_if_unsupported_mcp_version()

    key = _canonical_server(server)

    try:
        # Imported inside the try: the adapters touch major-specific modules, and
        # an import error must degrade to the no-op handle, not crash the host.
        from ._compatibility import (
            is_fastmcp,
            is_fastmcp_v2,
            is_low_level_server,
            is_mcpserver,
            uses_v2_handler_registry,
        )

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
            from ._instrument_fastmcp import instrument_fastmcp

            instrument_fastmcp(server, data)
        elif is_mcpserver(server):
            from ._instrument_v2 import instrument_mcpserver_v2

            instrument_mcpserver_v2(server, data)
        elif is_fastmcp_v2(server):
            from ._instrument_lowlevel import instrument_fastmcp_v2

            instrument_fastmcp_v2(server, data)
        elif is_low_level_server(server):
            if uses_v2_handler_registry(server):
                from ._instrument_v2 import instrument_lowlevel_v2

                instrument_lowlevel_v2(server, data)
            else:
                from ._instrument_lowlevel import instrument_low_level

                instrument_low_level(server, data)
        else:
            raise TypeError(
                f"Unsupported server type: {type(server)!r}. Pass a high-level server "
                "(mcp.server.fastmcp.FastMCP on SDK 1.x, mcp.server.mcpserver.MCPServer "
                "on SDK 2.x, or jlowin's fastmcp.FastMCP) or a low-level mcp.server.Server."
            )

        # Zero-config stateless minting: wrap the server's ASGI-app factories so a
        # stateless/multi-pod deployment keeps one $session_id + the client harness
        # across pods with no extra setup. No-op for stdio / low-level servers.
        autowire_stateless_mint(server)

        return McpAnalytics(key)
    except Exception as error:  # noqa: BLE001
        log(f"Warning: failed to instrument server - {error}")
        return _NoopAnalytics()
