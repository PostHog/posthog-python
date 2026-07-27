"""Self-encoded session tokens for stateless / multi-pod MCP servers.

A stateless server keeps nothing between requests, so every request starts a
new session and the client name/version (only sent at ``initialize``) is lost.
The one value clients replay on every request is the ``Mcp-Session-Id`` header.
So at ``initialize`` we mint that header as a token carrying the session id and
client identity -- any pod can read them back from the header alone.

The token is unsigned: it holds only what the client already self-reports. It is
wire-compatible with the TypeScript SDK (same short keys ``sid``/``cn``/``cv``/``pv``),
so a token minted by one SDK decodes in the other.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any, Optional

MCP_SESSION_HEADER = "mcp-session-id"

# On the wire the token is base64url(JSON) with shortened keys to keep the
# header small: sid = session_id, cn = client_name, cv = client_version,
# pv = protocol_version.
_MAX_TOKEN_LENGTH = 4096
_MAX_SESSION_ID_LENGTH = 128
_MAX_CLIENT_FIELD_LENGTH = 200

_BASE64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")


@dataclass
class SessionTokenPayload:
    """What a session token carries."""

    # PostHog session id (``ses_...``) -> ``$session_id``.
    session_id: str
    # MCP client name -> ``$mcp_client_name``.
    client_name: Optional[str] = None
    # MCP client version -> ``$mcp_client_version``.
    client_version: Optional[str] = None
    # MCP protocol (spec) version -> ``$mcp_protocol_version``. The client's
    # *requested* version -- the only one known when the token is minted (before
    # the initialize handshake negotiates). Lets pods that never saw ``initialize``
    # still stamp the spec version on their events.
    protocol_version: Optional[str] = None


def encode_session_id(payload: SessionTokenPayload) -> str:
    """Encode a session token for the ``Mcp-Session-Id`` response header.

    Raises ``ValueError`` for a missing/empty ``session_id`` (use ``new_session_id()``).
    """
    if not isinstance(payload.session_id, str) or not payload.session_id:
        raise ValueError(
            "encode_session_id requires a non-empty `session_id` (use new_session_id())"
        )
    wire: dict[str, str] = {"sid": payload.session_id}
    if isinstance(payload.client_name, str) and payload.client_name:
        wire["cn"] = payload.client_name[:_MAX_CLIENT_FIELD_LENGTH]
    if isinstance(payload.client_version, str) and payload.client_version:
        wire["cv"] = payload.client_version[:_MAX_CLIENT_FIELD_LENGTH]
    if isinstance(payload.protocol_version, str) and payload.protocol_version:
        wire["pv"] = payload.protocol_version[:_MAX_CLIENT_FIELD_LENGTH]
    raw = json.dumps(wire, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_session_id(value: Any) -> Optional[SessionTokenPayload]:
    """Decode an ``Mcp-Session-Id`` value into a token payload.

    Returns ``None`` for anything that isn't one of our tokens (transport UUIDs,
    JWTs, garbage) and never raises.
    """
    if not isinstance(value, str) or not value or len(value) > _MAX_TOKEN_LENGTH:
        return None
    # JWTs carry dots; UUIDs pass this check but fail JSON parsing below.
    if not _BASE64URL_PATTERN.match(value):
        return None
    try:
        parsed = json.loads(_base64url_to_bytes(value))
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    sid = parsed.get("sid")
    if not isinstance(sid, str) or not sid or len(sid) > _MAX_SESSION_ID_LENGTH:
        return None
    payload = SessionTokenPayload(session_id=sid)
    # A bad cn/cv/pv just means no client info -- it does not reject the token.
    cn = parsed.get("cn")
    if isinstance(cn, str) and cn:
        payload.client_name = cn[:_MAX_CLIENT_FIELD_LENGTH]
    cv = parsed.get("cv")
    if isinstance(cv, str) and cv:
        payload.client_version = cv[:_MAX_CLIENT_FIELD_LENGTH]
    pv = parsed.get("pv")
    if isinstance(pv, str) and pv:
        payload.protocol_version = pv[:_MAX_CLIENT_FIELD_LENGTH]
    return payload


def read_mcp_session_header(headers: Any) -> Optional[str]:
    """Read the ``mcp-session-id`` value off a headers mapping.

    Handles case-insensitive keys (transports lowercase them, but hand-built
    mappings may not) and list-valued headers, and trims whitespace.
    """
    if headers is None:
        return None
    value: Any = None
    # Mapping-like: prefer a direct get, else scan case-insensitively.
    get = getattr(headers, "get", None)
    if callable(get):
        value = get(MCP_SESSION_HEADER)
    if value is None:
        try:
            items = headers.items()
        except AttributeError:
            return None
        for key, candidate in items:
            if isinstance(key, str) and key.lower() == MCP_SESSION_HEADER:
                value = candidate
                break
    first = value[0] if isinstance(value, (list, tuple)) and value else value
    if not isinstance(first, str):
        return None
    trimmed = first.strip()
    return trimmed or None


def _base64url_to_bytes(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
