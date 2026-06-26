# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Detect which kind of MCP server was passed to ``instrument()``."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel import Server as LowLevelServer


def is_fastmcp(server: Any) -> bool:
    """The official SDK's high-level server (``mcp.server.fastmcp.FastMCP``)."""
    return isinstance(server, FastMCP)


def is_fastmcp_v2(server: Any) -> bool:
    """jlowin's standalone FastMCP 2.0 (``fastmcp.FastMCP``), a separate package
    from the official SDK. Returns False if ``fastmcp`` isn't installed."""
    try:
        from fastmcp import FastMCP as FastMCPv2
    except ImportError:
        return False
    return isinstance(server, FastMCPv2)


def is_low_level_server(server: Any) -> bool:
    return isinstance(server, LowLevelServer)
