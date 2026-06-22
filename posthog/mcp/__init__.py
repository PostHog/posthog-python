# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""PostHog MCP analytics SDK — product analytics for Model Context Protocol servers.

Wrap a Python MCP server (``FastMCP`` or low-level ``mcp.server.Server``) so every
tool call, agent intent, and failure is captured to PostHog as a ``$mcp_*`` event.

    from posthog.mcp import instrument
    analytics = instrument(server, posthog_client)

Requires the optional ``mcp`` dependency: ``pip install posthog[mcp]``.

(The server-wrapping API — ``instrument``, ``PostHogMCP`` — is added with the
server adapters. This module currently exposes the stable event vocabulary and
the STDIO-safe logger control, which need no ``mcp`` dependency.)
"""

from .constants import (
    POSTHOG_MCP_ANALYTICS_SOURCE,
    PostHogMCPAnalyticsEvent,
    PostHogMCPAnalyticsProperty,
)
from .logger import set_logger
from .version import __version__

__all__ = [
    "POSTHOG_MCP_ANALYTICS_SOURCE",
    "PostHogMCPAnalyticsEvent",
    "PostHogMCPAnalyticsProperty",
    "set_logger",
    "__version__",
]
