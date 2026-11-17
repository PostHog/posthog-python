"""Client conversation ids: a value an MCP client puts in a ``tools/call``
request's ``_meta`` to name the conversation the call belongs to.

``CLIENT_CONVERSATION_ROWS`` is the table of the spec's "Session identity"
requirement, row for row. A row's key is read only from a request its recognition
rule matches, so a generic key such as Codex's ``sessionId`` is never read from a
client that means something else by it. posthog-js keeps the same table.
"""

from __future__ import annotations

import re
from typing import Any, Callable, NamedTuple, Optional, Sequence

from ._transport_identity import CLIENT_USER_AGENT_HEADER
from .request_headers import _to_header_bag, get_request_headers
from .session_token import decode_session_id, read_mcp_session_header

__all__ = [
    "CLIENT_CONVERSATION_ROWS",
    "ClientConversation",
    "ClientConversationRow",
    "meta_client_name",
    "read_client_conversation",
    "recognize_client",
    "recognize_manual_client",
    "request_user_agent",
]


class ClientConversation(NamedTuple):
    """A recognized client's conversation id, and the ``_meta`` key it came from."""

    key: str
    id: str


class ClientConversationRow(NamedTuple):
    key: str
    read_value: Callable[[Any], Optional[str]]
    recognizes: Callable[[Optional[str], Optional[str]], bool]
    # The client sends its id on every call, so the SDK advertises no
    # ``conversation_id`` to it and mints it no handle. Rows whose clients have
    # versions or kinds of conversation that send no id keep the handle.
    handle_free: bool


_PRINTABLE_ASCII_UP_TO_128 = re.compile(r"[\x20-\x7e]{1,128}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_NIL_UUID = "00000000-0000-0000-0000-000000000000"
_CHATGPT_USER_AGENT = re.compile(r"openai-mcp/[^\s(]*\s*\(\s*chatgpt\s*[;,)]", re.I)
_VSCODE_PRODUCT_NAMES = ("Visual Studio Code", "Visual Studio Code - Insiders")


def _read_printable_ascii(value: Any) -> Optional[str]:
    if isinstance(value, str) and _PRINTABLE_ASCII_UP_TO_128.fullmatch(value):
        return value
    return None


def _read_uuid(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    lowered = value.lower()
    return lowered if _UUID.fullmatch(lowered) and lowered != _NIL_UUID else None


def _user_agent_starts_with(user_agent: Optional[str], prefixes: Sequence[str]) -> bool:
    lowered = (user_agent or "").lower()
    return bool(lowered) and any(lowered.startswith(p.lower()) for p in prefixes)


def _client_name_is(client_name: Optional[str], names: Sequence[str]) -> bool:
    lowered = (client_name or "").lower()
    return bool(lowered) and any(lowered == name.lower() for name in names)


# Checked in this order; the first row that recognizes a request is its client's row.
CLIENT_CONVERSATION_ROWS = (
    ClientConversationRow(
        key="openai/session",
        read_value=_read_printable_ascii,
        recognizes=lambda user_agent, _: bool(
            user_agent and _CHATGPT_USER_AGENT.match(user_agent)
        ),
        handle_free=True,
    ),
    ClientConversationRow(
        key="sessionId",
        read_value=_read_uuid,
        recognizes=lambda user_agent, client_name: (
            _user_agent_starts_with(user_agent, ["codex-mcp-client/"])
            or _client_name_is(client_name, ["codex-mcp-client"])
        ),
        handle_free=False,
    ),
    ClientConversationRow(
        key="vscode.conversationId",
        read_value=_read_uuid,
        recognizes=lambda user_agent, client_name: (
            _user_agent_starts_with(
                user_agent, [f"{name}/" for name in _VSCODE_PRODUCT_NAMES]
            )
            or _client_name_is(client_name, _VSCODE_PRODUCT_NAMES)
        ),
        handle_free=False,
    ),
)


def recognize_client(
    user_agent: Optional[str], client_name: Optional[str]
) -> Optional[ClientConversationRow]:
    """The row of the client a request's ``User-Agent`` or client name identifies.

    The client name is ``$mcp_client_name`` as the adapters resolve it for the
    request: its own ``_meta``, then a replayed session token, then a handshake."""
    user_agent = user_agent if isinstance(user_agent, str) else None
    client_name = client_name if isinstance(client_name, str) else None
    for row in CLIENT_CONVERSATION_ROWS:
        if row.recognizes(user_agent, client_name):
            return row
    return None


def read_client_conversation(
    row: Optional[ClientConversationRow], meta: Any
) -> Optional[ClientConversation]:
    """The recognized client's conversation id in ``meta``, or ``None`` when there
    is none or it is not the row's shape."""
    if row is None or not isinstance(meta, dict):
        return None
    value = row.read_value(meta.get(row.key))
    return ClientConversation(row.key, value) if value is not None else None


def request_user_agent(extra: Any) -> Optional[str]:
    """The request's ``User-Agent``, or ``None`` off HTTP. Never raises."""
    try:
        headers = get_request_headers(extra)
    except Exception:  # noqa: BLE001 - a header read must not break a request
        return None
    value = (headers or {}).get(CLIENT_USER_AGENT_HEADER)
    return value if isinstance(value, str) else None


def recognize_manual_client(
    headers: Any, request_meta: Any, handshake_client_name: Optional[str]
) -> Optional[ClientConversationRow]:
    """:func:`recognize_client` for a custom dispatcher, from what it passes in:
    the request's headers, its ``_meta``, and a handshake ``clientInfo`` name
    bound to the request's transport session. The client name follows the
    request's ``_meta``, then a replayed session token, then the handshake."""
    bag = _to_header_bag(headers) if headers is not None else None
    user_agent = (bag or {}).get(CLIENT_USER_AGENT_HEADER)
    token = decode_session_id(read_mcp_session_header(bag)) if bag else None
    return recognize_client(
        user_agent if isinstance(user_agent, str) else None,
        meta_client_name(request_meta)
        or (token.client_name if token is not None else None)
        or handshake_client_name,
    )


def meta_client_name(meta: Any) -> Optional[str]:
    """The client name a 2026-07-28 request carries in its own ``_meta``."""
    info = (
        meta.get("io.modelcontextprotocol/clientInfo")
        if isinstance(meta, dict)
        else None
    )
    name = info.get("name") if isinstance(info, dict) else None
    return name if isinstance(name, str) and name else None
