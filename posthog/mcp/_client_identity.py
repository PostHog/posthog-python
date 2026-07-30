"""Client identity under the MCP 2026-07-28 (stateless) revision.

That revision removes the ``initialize`` handshake and the ``Mcp-Session-Id``
header (SEP-2575 / SEP-2567). Client name/version and the protocol version no
longer arrive once at ``initialize`` — they travel in every request's
``params._meta`` under the reverse-DNS keys below. We mirror the literal key
strings here rather than depend on the (still beta) v2 SDK, which is not a
dependency of this package.

Reading them per request also keeps identity correct when one instrumented
server multiplexes concurrent requests from different clients, which the
stateless spec allows: each request resolves its own identity instead of
sharing server-wide state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

META_CLIENT_INFO_KEY = "io.modelcontextprotocol/clientInfo"
META_PROTOCOL_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"

# Depth cap for the source walk below; the real nesting is at most
# context -> request_context -> params -> meta.
_MAX_UNWRAP_DEPTH = 6


@dataclass
class MetaClientInfo:
    """Whatever slice of the client identity a request's ``_meta`` carried."""

    client_name: Optional[str] = None
    client_version: Optional[str] = None
    protocol_version: Optional[str] = None


def _non_empty_str(value: Any) -> Optional[str]:
    """Keep only non-empty strings — a client sending ``""`` or a non-string
    shouldn't blank out an otherwise-good value from the token or ``initialize``."""
    return value if isinstance(value, str) and value else None


def _extract_meta(source: Any, depth: int = 0) -> Optional[Mapping[str, Any]]:
    """Best-effort walk to the ``_meta`` mapping.

    Accepts any of the shapes the call sites have on hand: a FastMCP ``Context``,
    a low-level ``RequestContext``, an MCP request object, a request params
    object, the ``RequestParams.Meta`` model itself, or a plain dict of any of
    those. Returns ``None`` when the source carries no ``_meta``."""
    if source is None or depth > _MAX_UNWRAP_DEPTH:
        return None

    if isinstance(source, Mapping):
        # A JSON-RPC-ish request dict: descend into its params.
        if "params" in source or "method" in source:
            return _extract_meta(source.get("params"), depth + 1)
        # Params: the wire spells this `_meta`, but `request_to_dict` dumps
        # pydantic params without `by_alias`, so it comes through as `meta`.
        for key in ("_meta", "meta"):
            nested = source.get(key)
            if isinstance(nested, Mapping):
                return nested
        # Otherwise assume we were handed the meta mapping directly.
        return source

    # Descend toward the meta object first. Request and params models are
    # `extra="allow"` as well, so checking `model_extra` before descending would
    # match the wrong (and normally empty) level and stop the walk early.
    for attr in ("meta", "params", "request_context"):
        try:
            nested = getattr(source, attr, None)
        except Exception:  # noqa: BLE001 - property access can raise off-request
            nested = None
        if nested is not None and nested is not source:
            found = _extract_meta(nested, depth + 1)
            if found is not None:
                return found

    # `RequestParams.Meta` is `extra="allow"`, so the reverse-DNS keys — which
    # aren't declared fields — land in `model_extra`.
    extra = getattr(source, "model_extra", None)
    if isinstance(extra, Mapping) and extra:
        return extra
    return None


def read_meta_client_info(source: Any) -> Optional[MetaClientInfo]:
    """Read the client name/version and protocol version a modern client puts in
    ``params._meta``. Returns ``None`` when the request carries none (e.g. a
    legacy client, which sends this at ``initialize`` instead). Never raises."""
    try:
        meta = _extract_meta(source)
    except Exception:  # noqa: BLE001 - identity is best-effort, never fatal
        return None
    if not isinstance(meta, Mapping):
        return None

    info = MetaClientInfo()

    client_info = meta.get(META_CLIENT_INFO_KEY)
    if isinstance(client_info, Mapping):
        info.client_name = _non_empty_str(client_info.get("name"))
        info.client_version = _non_empty_str(client_info.get("version"))
    elif client_info is not None:
        info.client_name = _non_empty_str(getattr(client_info, "name", None))
        info.client_version = _non_empty_str(getattr(client_info, "version", None))

    info.protocol_version = _non_empty_str(meta.get(META_PROTOCOL_VERSION_KEY))

    if not (info.client_name or info.client_version or info.protocol_version):
        return None
    return info


def apply_meta_client_info(
    source: Any,
    client_name: Optional[str],
    client_version: Optional[str],
    protocol_version: Optional[str],
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Layer any ``_meta`` client identity over values resolved from the transport
    or the session token.

    ``_meta`` wins: it is the per-request truth under the 2026-07-28 revision,
    where there is no ``initialize`` and no session token to learn from. Only the
    fields the request actually carries are overridden, so a legacy request
    (no ``_meta``) leaves all three untouched."""
    info = read_meta_client_info(source)
    if info is None:
        return client_name, client_version, protocol_version
    return (
        info.client_name or client_name,
        info.client_version or client_version,
        info.protocol_version or protocol_version,
    )
