# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Which generation of the official ``mcp`` SDK is installed.

The 2026-07-28 spec ships as ``mcp`` 2.x, a breaking rewrite of the same PyPI
package (``mcp.server.fastmcp`` gone, low-level handlers now keyed by method
string, an official middleware protocol). The two generations can't coexist in
one venv, so ``instrument()`` and the test suite both branch on this probe rather
than on a try/except over a module import that means different things per version.
"""

from __future__ import annotations

from typing import Literal, Optional


def installed_mcp_generation() -> Optional[Literal[1, 2]]:
    """The major version of the installed ``mcp`` SDK as a generation number:
    ``1`` for ``mcp>=1,<2`` (the handshake-era SDK), ``2`` for ``mcp>=2,<3`` (the
    2026-07-28 SDK), or ``None`` when ``mcp`` isn't installed or its version is
    unreadable/outside the supported range. Never raises."""
    try:
        from importlib.metadata import version

        major = int(version("mcp").split(".")[0])
    except Exception:  # noqa: BLE001 - a probe must never break its callers
        return None
    if major == 1:
        return 1
    if major == 2:
        return 2
    return None
