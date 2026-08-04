# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Detect which kind of MCP server was passed to ``instrument()``.

Every check imports lazily and swallows import failures: the ``mcp`` SDK ships
two mutually incompatible generations under one package name (1.x removed
``mcp.server.fastmcp`` in 2.x, which added ``mcp.server.mcpserver.MCPServer``),
so a module-level import of either would crash ``instrument()`` on the other
generation. Each predicate returns ``False`` cleanly when its target class isn't
importable, letting ``instrument()`` dispatch on whichever generation is present.
"""

from __future__ import annotations

from typing import Any


def is_fastmcp(server: Any) -> bool:
    """The v1 SDK's high-level server (``mcp.server.fastmcp.FastMCP``). Returns
    False on mcp 2.x, where that module was removed."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        return False
    return isinstance(server, FastMCP)


def is_fastmcp_v2(server: Any) -> bool:
    """jlowin's standalone FastMCP 2.0 (``fastmcp.FastMCP``), a separate package
    from the official SDK. Its server layer needs mcp 1.x internals, so the import
    raises under mcp 2.x — returns False there (and when ``fastmcp`` is absent)."""
    try:
        from fastmcp import FastMCP as FastMCPv2
    except ImportError:
        return False
    return isinstance(server, FastMCPv2)


def is_mcpserver_v2(server: Any) -> bool:
    """The mcp 2.x high-level server (``mcp.server.mcpserver.MCPServer``), which
    replaced ``FastMCP``. Returns False on mcp 1.x, where that module is absent."""
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError:
        return False
    return isinstance(server, MCPServer)


def is_low_level_server(server: Any) -> bool:
    """A raw ``mcp.server.lowlevel.Server``. Present in both generations, but its
    handler seam differs (v1: public ``request_handlers`` keyed by request type;
    v2: private ``_request_handlers`` keyed by method string), so callers must
    branch on generation before wrapping it."""
    try:
        from mcp.server.lowlevel import Server as LowLevelServer
    except ImportError:
        return False
    return isinstance(server, LowLevelServer)
