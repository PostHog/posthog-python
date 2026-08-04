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
from .session import derive_session_id_from_mcp_session, new_session_id
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
    "derive_session_id_from_mcp_session",
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
    """PostHog MCP analytics supports two generations of the ``mcp`` SDK: 1.x
    (``mcp>=1.26,<2``, the ``request_handlers`` seam) and 2.x (``mcp>=2,<3``, the
    2026-07-28 ``ServerMiddleware`` seam). Since ``mcp`` is an unpinned peer
    dependency, advise at runtime when the installed version is outside both
    supported ranges rather than failing hard (a near-neighbor may still work)."""
    try:
        from importlib.metadata import version

        installed = version("mcp")
        major, minor = (int(p) for p in installed.split(".")[:2])
    except Exception:  # noqa: BLE001 - never let a version probe break instrument()
        return
    if (major, minor) < (1, 26) or major >= 3:
        log(
            f"Warning: PostHog MCP analytics supports mcp>=1.26,<2 and mcp>=2,<3; found {installed}. "
            "Instrumentation may behave unexpectedly on this version."
        )


def _canonical_server(server: Any) -> Any:
    """The underlying low-level server for high-level wrappers, else the server
    itself. v1 FastMCP (official and jlowin's) exposes ``_mcp_server``; the mcp 2.x
    ``MCPServer`` exposes ``_lowlevel_server``. Used as the tracking key so
    instrumenting a wrapper and its underlying server resolve to one state instead
    of two divergent ones (matching the TS SDK)."""
    low_level = getattr(server, "_mcp_server", None) or getattr(
        server, "_lowlevel_server", None
    )
    return low_level if low_level is not None else server


def _instrument_generation_1(
    server: Any,
    data: MCPAnalyticsData,
    is_fastmcp: Any,
    is_fastmcp_v2: Any,
    is_low_level_server: Any,
) -> None:
    """Dispatch for the mcp 1.x SDK: the ``request_handlers`` monkey-patch seam
    plus zero-config stateless minting (an ASGI wrap that is a no-op for stdio /
    low-level servers)."""
    from ._instrument_fastmcp import instrument_fastmcp
    from ._instrument_lowlevel import instrument_fastmcp_v2, instrument_low_level

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

    # Zero-config stateless minting: wrap the server's ASGI-app factories so a
    # stateless/multi-pod deployment keeps one $session_id + the client harness
    # across pods with no extra setup. No-op for stdio / low-level servers.
    autowire_stateless_mint(server)


def _instrument_generation_2(server: Any, data: MCPAnalyticsData) -> None:
    """Dispatch for the mcp 2.x SDK (2026-07-28): attach the analytics
    ``ServerMiddleware`` to the ``MCPServer`` or low-level ``Server``. A capture-only
    adapter — no context injection, no tool-list mutation, no stateless minting
    (SEP-2567 removed the ``Mcp-Session-Id`` header, so there's nothing to mint)."""
    from ._compatibility import is_low_level_server, is_mcpserver_v2
    from ._instrument_v2 import instrument_low_level_v2, instrument_mcpserver_v2

    if is_mcpserver_v2(server):
        instrument_mcpserver_v2(server, data)
    elif is_low_level_server(server):
        instrument_low_level_v2(server, data)
    else:
        raise TypeError(
            f"Unsupported server type for mcp 2.x: {type(server)!r}. Pass an "
            "mcp.server.mcpserver.MCPServer or a low-level mcp.server.lowlevel.Server."
        )


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
    from ._mcp_version import installed_mcp_generation
    from ._compatibility import is_fastmcp, is_fastmcp_v2, is_low_level_server

    generation = installed_mcp_generation()
    key = _canonical_server(server)

    try:
        client = _resolve_client(posthog_client)
        if client is None:
            log("Warning: no PostHog client available; MCP events will not be sent.")

        if get_server_tracking_data(key) is not None:
            log("instrument() - server already instrumented, skipping initialization")
            return McpAnalytics(key)

        sink = McpEventSink(client) if client is not None else None
        data = MCPAnalyticsData(options=opts, sink=sink, session_id=new_session_id())
        set_server_tracking_data(key, data)

        if generation == 2:
            _instrument_generation_2(server, data)
        else:
            _instrument_generation_1(
                server, data, is_fastmcp, is_fastmcp_v2, is_low_level_server
            )

        return McpAnalytics(key)
    except Exception as error:  # noqa: BLE001
        # Degrade to a no-op so the host app keeps working, but make the failure
        # actionable: name what happened, the detected generation, and the
        # versions we support — never a bare ModuleNotFoundError or silent no-op.
        log(
            f"Warning: failed to instrument server (mcp generation {generation}, "
            f"supported: 1.x as mcp>=1.26,<2 and 2.x as mcp>=2,<3) - {error}"
        )
        return _NoopAnalytics()
