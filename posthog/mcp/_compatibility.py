# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Detect which kind of MCP server was passed to ``instrument()``.

Every probe is import-tolerant: the classes live at different paths per MCP SDK
major (``mcp.server.fastmcp.FastMCP`` on 1.x, ``mcp.server.mcpserver.MCPServer``
on 2.x), so a probe whose class doesn't exist on the installed major answers
``False`` instead of raising — an unconditional import here is exactly what
would crash ``instrument()`` on the other major.
"""

from __future__ import annotations

from typing import Any


def is_fastmcp(server: Any) -> bool:
    """The MCP SDK 1.x high-level server (``mcp.server.fastmcp.FastMCP``).
    The module was renamed in 2.x, so this is False whenever mcp>=2 is installed."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        return False
    return isinstance(server, FastMCP)


def is_mcpserver(server: Any) -> bool:
    """The MCP SDK 2.x high-level server (``mcp.server.mcpserver.MCPServer``,
    the renamed FastMCP). False whenever mcp<2 is installed."""
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError:
        return False
    return isinstance(server, MCPServer)


def is_fastmcp_v2(server: Any) -> bool:
    """jlowin's standalone FastMCP 2.0 (``fastmcp.FastMCP``), a separate package
    from the official SDK. Returns False if ``fastmcp`` isn't installed."""
    try:
        from fastmcp import FastMCP as FastMCPv2
    except ImportError:
        return False
    return isinstance(server, FastMCPv2)


def is_low_level_server(server: Any) -> bool:
    """The low-level ``mcp.server.lowlevel.Server`` — the import path is the
    same on both majors; use :func:`uses_v2_handler_registry` to tell which
    handler seam it carries."""
    try:
        from mcp.server.lowlevel import Server as LowLevelServer
    except ImportError:
        return False
    return isinstance(server, LowLevelServer)


def uses_v2_handler_registry(server: Any) -> bool:
    """Which major's handler seam a low-level server carries, decided by shape
    rather than package version (a la posthog-js ADR-0005): 1.x exposes the
    public ``request_handlers`` dict keyed by request class; 2.x replaced it
    with ``add_request_handler``/``get_request_handler`` keyed by method string."""
    if hasattr(server, "add_request_handler") and hasattr(
        server, "get_request_handler"
    ):
        return True
    return not hasattr(server, "request_handlers")
