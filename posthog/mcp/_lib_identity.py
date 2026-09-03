"""Apply the MCP-specific identity to the underlying PostHog client."""

from __future__ import annotations

from ..client import Client

from .constants import POSTHOG_MCP_LIB_NAME
from .version import __version__


def apply_mcp_lib_identity(client: Client) -> None:
    """Relabel every event sent by ``client`` as coming from ``posthog.mcp``."""
    set_identity = getattr(client, "_set_library_identity", None)
    if set_identity is not None:
        set_identity(POSTHOG_MCP_LIB_NAME, __version__)
