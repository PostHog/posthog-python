# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Detect which kind of MCP server was passed to ``instrument()``."""

from __future__ import annotations

from typing import Any

from mcp.server.lowlevel import Server as LowLevelServer

# The official SDK's high-level server, under both names it has shipped under:
# `mcp.server.fastmcp.FastMCP` on 1.x, renamed `mcp.server.mcpserver.MCPServer`
# in mcp>=2. Only one of these resolves on any given install, so import both
# tolerantly — a hard import of either breaks `posthog.mcp` on the other major.
_HIGH_LEVEL_SERVERS: tuple = ()
for _module, _name in (
    ("mcp.server.fastmcp", "FastMCP"),
    ("mcp.server.mcpserver", "MCPServer"),
):
    try:
        _HIGH_LEVEL_SERVERS += (getattr(__import__(_module, fromlist=[_name]), _name),)
    except Exception:  # noqa: BLE001 - absent on the other major version
        pass


def is_fastmcp(server: Any) -> bool:
    """The official SDK's high-level server, on either major version."""
    return bool(_HIGH_LEVEL_SERVERS) and isinstance(server, _HIGH_LEVEL_SERVERS)


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
