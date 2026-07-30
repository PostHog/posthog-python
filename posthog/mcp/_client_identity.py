"""Client identity under the MCP 2026-07-28 (stateless) revision.

That revision removes the ``initialize`` handshake and the ``Mcp-Session-Id``
header (SEP-2575 / SEP-2567), so the client name/version and the protocol
version travel in every request's ``params._meta`` under the reverse-DNS keys
below instead of arriving once at ``initialize``. We mirror the literal key
strings rather than depend on the (still beta) v2 SDK, which is not a
dependency of this package.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

META_CLIENT_INFO_KEY = "io.modelcontextprotocol/clientInfo"
META_PROTOCOL_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"


def _text(value: Any) -> Optional[str]:
    """Non-empty strings only, so a client sending ``""`` or a non-string can't
    blank out a good value from the transport or the session token."""
    return value if isinstance(value, str) and value else None


def _meta_entries(source: Any) -> Optional[Mapping[str, Any]]:
    """The request's ``_meta`` entries, or ``None``.

    ``source`` is either a ``RequestContext`` (which carries it as ``.meta``) or
    a request object (which nests it under ``.params``)."""
    try:
        meta = getattr(source, "meta", None) or getattr(
            getattr(source, "params", None), "meta", None
        )
        # `RequestParams.Meta` is `extra="allow"`, and these reverse-DNS keys
        # aren't declared fields, so they land in `model_extra`.
        extra = getattr(meta, "model_extra", None)
    except Exception:  # noqa: BLE001 - identity is best-effort, never fatal
        return None
    return extra if isinstance(extra, Mapping) else None


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
    meta = _meta_entries(source)
    if not meta:
        return client_name, client_version, protocol_version

    client_info = meta.get(META_CLIENT_INFO_KEY)
    if isinstance(client_info, Mapping):
        client_name = _text(client_info.get("name")) or client_name
        client_version = _text(client_info.get("version")) or client_version

    return (
        client_name,
        client_version,
        _text(meta.get(META_PROTOCOL_VERSION_KEY)) or protocol_version,
    )
