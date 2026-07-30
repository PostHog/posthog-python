"""Client identity under the MCP 2026-07-28 (stateless) revision.

That revision removes the ``initialize`` handshake and the ``Mcp-Session-Id``
header (SEP-2575 / SEP-2567), so the client name/version and the protocol
version travel in every request's ``params._meta`` under the reverse-DNS keys
below instead of arriving once at ``initialize``.

We spell the keys out rather than import them: ``mcp>=2`` exports them as
``CLIENT_INFO_META_KEY`` / ``PROTOCOL_VERSION_META_KEY``, but ``mcp`` is an
optional peer dependency here (``PostHogMCP`` works without it) and 1.x has no
such constants. The strings match the 2026-07-28 schema either way.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

META_CLIENT_INFO_KEY = "io.modelcontextprotocol/clientInfo"
META_PROTOCOL_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"


def _str(value: Any) -> Optional[str]:
    """Non-empty strings only, so a client sending ``""`` or a non-string can't
    blank out (or put junk over) a good value from the token or ``initialize``."""
    return value if isinstance(value, str) and value else None


def apply_meta_client_info(
    source: Any,
    client_name: Optional[str],
    client_version: Optional[str],
    protocol_version: Optional[str],
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Layer any client identity from this request's ``_meta`` over values already
    resolved from the transport or the session token.

    ``_meta`` wins: under the 2026-07-28 revision there is no ``initialize`` and
    no session token to learn from, so it is the only per-request truth. Only the
    fields the request actually carries override, so a legacy request (no
    ``_meta``) leaves all three untouched."""
    try:
        # A RequestContext carries `_meta` directly; a request nests it under
        # `params`.
        params = getattr(source, "params", None)
        meta = getattr(source, "meta", None) or getattr(params, "meta", None)
        # `mcp>=2` hands it over as a plain dict. On 1.x it's a
        # `RequestParams.Meta` model instead, and since that model is
        # `extra="allow"`, these undeclared reverse-DNS keys land in
        # `model_extra` — the dict we actually want.
        if not isinstance(meta, Mapping):
            meta = getattr(meta, "model_extra", None) or {}

        client_info = meta.get(META_CLIENT_INFO_KEY) or {}
        return (
            _str(client_info.get("name")) or client_name,
            _str(client_info.get("version")) or client_version,
            _str(meta.get(META_PROTOCOL_VERSION_KEY)) or protocol_version,
        )
    except Exception:  # noqa: BLE001 - `_meta` is client-controlled; never fatal
        return client_name, client_version, protocol_version
