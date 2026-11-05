# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Session id resolution: prefer a transport-supplied MCP session id (derived
deterministically so it survives restarts) over an SDK-generated one, which
rolls over after an inactivity timeout."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping, NamedTuple, Optional

from .constants import INACTIVITY_TIMEOUT_IN_MINUTES
from ._ids import deterministic_prefixed_id, new_prefixed_id
from ._internal import MCPAnalyticsData
from .session_token import SessionTokenPayload

__all__ = [
    "derive_session_id_from_mcp_session",
    "derive_session_id_from_conversation",
    "derive_session_id_from_client_conversation",
]


class ResolvedSession(NamedTuple):
    """A request's session id and where it came from: ``"conversation"``,
    ``"token"``, ``"mcp"``, ``"client_metadata"`` or ``"generated"``."""

    session_id: str
    source: str


class ClientConversation(NamedTuple):
    """A host client's own conversation id, and the ``_meta`` key it came from."""

    key: str
    id: str


# Host clients that put their own conversation id in a request's ``_meta``, by key.
# ChatGPT documents ``openai/session`` as an anonymized id "for correlating tool
# calls within the same ChatGPT session".
CLIENT_CONVERSATION_META_KEYS = ("openai/session",)

# Printable ASCII only, so every SDK hashes the same characters.
_CLIENT_CONVERSATION_ID_SHAPE = re.compile(r"[\x20-\x7e]{1,256}")


def read_client_conversation(meta: Any) -> Optional[ClientConversation]:
    """The first recognized client conversation id in a request's ``_meta``."""
    if not isinstance(meta, dict):
        return None
    for key in CLIENT_CONVERSATION_META_KEYS:
        value = meta.get(key)
        if isinstance(value, str) and _CLIENT_CONVERSATION_ID_SHAPE.fullmatch(value):
            return ClientConversation(key, value)
    return None


def new_session_id() -> str:
    return new_prefixed_id("ses")


def derive_session_id_from_mcp_session(mcp_session_id: str) -> str:
    """Deterministic SDK session id for an MCP protocol session, so the same MCP
    session correlates to one ``$session_id`` across server restarts."""
    return deterministic_prefixed_id("ses", mcp_session_id)


def derive_session_id_from_conversation(conversation_id: str) -> str:
    """Derive the SDK session id from the agent's conversation handle.

    Deterministic and unsalted on purpose: two pods that never met must agree on
    the session, and the 2026-07-28 protocol revision leaves them no shared
    state to agree through. Hashed rather than used verbatim so an MCP session
    can never collide with a Session Replay id.

    This is the cross-SDK contract: posthog-js's ``deriveSessionIdFromConversation``
    produces the same value byte for byte, or the same conversation splits into
    two sessions depending on which SDK served the call.
    """
    return deterministic_prefixed_id("ses", conversation_id)


def derive_session_id_from_client_conversation(key: str, id: str) -> str:
    """Derive the SDK session id from a host client's conversation id.

    Keyed so two hosts' ids, or a host id and a conversation handle, never share
    a hash input. Part of the cross-SDK contract, like
    :func:`derive_session_id_from_conversation`: posthog-js produces the same value
    byte for byte (``openai/session`` + ``v1/aBcDeF123`` is
    ``ses_35e4cc45ea3e5e1282c3f091de632132``)."""
    return deterministic_prefixed_id("ses", f"{key}:{id}")


async def resolve_session_id(
    data: MCPAnalyticsData,
    mcp_session_id: Optional[str],
    *,
    token: Optional[SessionTokenPayload] = None,
    conversation_id: Optional[str] = None,
    client_conversation: Optional[ClientConversation] = None,
) -> str:
    """The session id for this request. See :func:`resolve_session_id_with_source`,
    which this wraps -- callers that need to know *where* the session came from
    should use that instead of reading ``data.session_source`` afterwards."""
    session_id, _ = await resolve_session_id_with_source(
        data,
        mcp_session_id,
        token=token,
        conversation_id=conversation_id,
        client_conversation=client_conversation,
    )
    return session_id


async def handshake_session(
    data: MCPAnalyticsData,
    mcp_session_id: Optional[str],
    token: Optional[SessionTokenPayload],
    params: Mapping[str, Any],
) -> ResolvedSession:
    """The session an ``initialize`` belongs to: the one the client's later requests
    resolve to through :func:`resolve_session_id_with_source`.

    A token or a transport session id gives it directly, so this leaves the server's
    shared session alone. One server serves many clients, and a handshake must not
    move the session that ``McpAnalytics.capture()`` reads. Without either, the
    client's own conversation id (in the ``initialize`` params' ``_meta``) or this
    process's own session applies, which only the resolver can hand out."""
    if token is not None:
        return ResolvedSession(token.session_id, "token")
    if mcp_session_id:
        return ResolvedSession(
            derive_session_id_from_mcp_session(mcp_session_id), "mcp"
        )
    return await resolve_session_id_with_source(
        data, None, client_conversation=read_client_conversation(params.get("_meta"))
    )


async def resolve_session_id_with_source(
    data: MCPAnalyticsData,
    mcp_session_id: Optional[str],
    *,
    token: Optional[SessionTokenPayload] = None,
    conversation_id: Optional[str] = None,
    client_conversation: Optional[ClientConversation] = None,
) -> ResolvedSession:
    """Resolve the session id for a request. Mutates per-server state under a lock
    so concurrent async requests can't race on session rotation.

    Returns ``(session_id, source)`` where source is one of ``"conversation"``,
    ``"token"``, ``"mcp"``, ``"client_metadata"`` or ``"generated"`` -- describing *this* request. Callers
    must not infer it from ``data.session_source`` instead: that field is shared
    mutable state which the conversation branch below deliberately never writes, so
    reading it after the fact reports whatever some earlier request happened to
    leave there.

    Priority mirrors posthog-js ``getSessionId``: the agent's ``conversation_id``
    handle first (the only id that survives the 2026-07-28 revision's
    per-request server instances), then our self-encoded session token, then the
    transport's MCP session id, then the host client's own conversation id (ADR-0014
    in posthog-js), then this instance's own memory. The client id ranks below the
    first three so it only groups calls that would otherwise fragment, and never
    regroups a session they already hold.

    ``conversation_id`` and ``client_conversation`` are resolved *per request and
    never stored*: each belongs to one chat, and persisting it on shared ``data`` (or
    advancing ``last_activity``) would leak one chat's session onto a concurrent chat's
    request.

    ``token`` is our self-encoded session token (see :mod:`.session_token`),
    decoded from the replayed ``Mcp-Session-Id`` header. It is the only other
    source that survives a stateless / multi-pod deployment.

    The token session is resolved *per request*, never sticky: ``data`` is shared
    by every client hitting this server instance, so reusing a stored token session
    for a request that didn't replay the token would merge unrelated clients under
    one ``$session_id``. A compliant client replays the header on every request, so
    a genuine token session never needs the fallback.
    """
    if conversation_id:
        return ResolvedSession(
            derive_session_id_from_conversation(conversation_id), "conversation"
        )

    if token is None and not mcp_session_id and client_conversation is not None:
        return ResolvedSession(
            derive_session_id_from_client_conversation(*client_conversation),
            "client_metadata",
        )

    async with data.session_lock:
        now = datetime.now(timezone.utc)

        if token is not None:
            # Its session id is already a `ses_...` id, so use it verbatim -- do
            # NOT re-hash. (Client name/version are recovered per request in the
            # adapters, not stored on shared `data`.)
            data.session_id = token.session_id
            data.session_source = "token"
            data.last_activity = now
            return ResolvedSession(data.session_id, "token")

        if mcp_session_id:
            data.session_id = derive_session_id_from_mcp_session(mcp_session_id)
            data.last_mcp_session_id = mcp_session_id
            data.session_source = "mcp"
            data.last_activity = now
            return ResolvedSession(data.session_id, "mcp")

        # Once a session is MCP-derived, keep it even if a later request arrives
        # without the MCP session id, so the session doesn't fragment.
        if data.session_source == "mcp" and data.last_mcp_session_id:
            data.last_activity = now
            return ResolvedSession(data.session_id, "mcp")

        # Memory fallback (single-owner transports like stdio). A leftover token
        # session must NOT leak to a credential-less request, so anything that
        # isn't already a generated session starts fresh; generated sessions
        # persist and roll over on inactivity.
        timeout_seconds = INACTIVITY_TIMEOUT_IN_MINUTES * 60
        is_stale = (now - data.last_activity).total_seconds() > timeout_seconds
        if data.session_source != "generated" or is_stale:
            data.session_id = new_session_id()
            data.session_source = "generated"
        data.last_activity = now
        return ResolvedSession(data.session_id, "generated")
