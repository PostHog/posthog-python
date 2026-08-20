"""Fixtures for the MCP Python SDK v2 (``mcp>=2``) tests.

Only imported by the ``test_v2_*`` files, which ``conftest.py`` excludes from
collection under mcp 1.x — so this module may assume v2 symbols exist.

v2 low-level handlers receive ``(ctx, params)`` where ``ctx`` is a
``ServerRequestContext``. The adapters only read a handful of attributes from
it (``protocol_version``, ``session.client_params``, ``request.headers``), all
defensively, so a ``SimpleNamespace`` with the same shape drives the wrapped
handlers without standing up a session or transport.
"""

from types import SimpleNamespace
from typing import Any, Dict, Optional


def fake_ctx(
    protocol_version: str = "2026-07-28",
    client_name: Optional[str] = "test-client",
    client_version: Optional[str] = "9.9.9",
    method: str = "tools/call",
    headers: Optional[Dict[str, str]] = None,
) -> Any:
    """A ``ServerRequestContext``-shaped stand-in, as the v2 runner would build.

    ``client_params`` mirrors v2's snake_case ``InitializeRequestParams``
    (synthesized from the per-request envelope on the modern era, or from the
    handshake on the legacy era).
    """
    client_params = None
    if client_name is not None:
        client_params = SimpleNamespace(
            client_info=SimpleNamespace(name=client_name, version=client_version),
            protocol_version=protocol_version,
        )
    session = SimpleNamespace(client_params=client_params)
    request = SimpleNamespace(headers=headers) if headers is not None else None
    return SimpleNamespace(
        session=session,
        protocol_version=protocol_version,
        method=method,
        params=None,
        request_id=1,
        meta=None,
        request=request,
    )


# --- wire-level helpers (test_v2_wire_dual_era) --------------------------------

MODERN_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"


def modern_meta(
    client_name: str = "wire-client", client_version: str = "1.2.3"
) -> Dict[str, Any]:
    """The 2026-07-28 per-request ``_meta`` envelope (protocol version and
    client capabilities are required; client info is a SHOULD)."""
    return {
        "io.modelcontextprotocol/protocolVersion": MODERN_PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {
            "name": client_name,
            "version": client_version,
        },
    }


def modern_headers(method: str, tool_name: Optional[str] = None) -> Dict[str, str]:
    """Required modern-era HTTP headers: the protocol version must match the
    envelope, ``Mcp-Method`` the body method, and ``Mcp-Name`` the tool name
    on name-bearing methods (SEP-2243)."""
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": MODERN_PROTOCOL_VERSION,
        "mcp-method": method,
    }
    if tool_name is not None:
        headers["mcp-name"] = tool_name
    return headers


def legacy_headers() -> Dict[str, str]:
    return {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
