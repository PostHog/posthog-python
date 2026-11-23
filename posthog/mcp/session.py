# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Session id resolution: prefer a transport-supplied MCP session id (derived
deterministically so it survives restarts) over an SDK-generated one, which
rolls over after an inactivity timeout."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import Context, ContextVar, copy_context
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Mapping, NamedTuple, Optional, Tuple

from .constants import INACTIVITY_TIMEOUT_IN_MINUTES
from ._client_conversation import ClientConversation
from ._ids import deterministic_prefixed_id, new_prefixed_id
from .logger import log
from ._internal import MCPAnalyticsData
from .session_token import SessionTokenPayload

__all__ = [
    "derive_session_id_from_mcp_session",
    "derive_session_id_from_conversation",
    "derive_session_id_from_client_conversation",
]


class ResolvedSession(NamedTuple):
    """A request's session id and where it came from: ``"conversation"``,
    ``"token"``, ``"mcp"``, ``"client_metadata"``, ``"request"`` (generated for
    one sessionless HTTP request) or ``"generated"``."""

    session_id: str
    source: str


# A call holds its custom events until it settles; beyond this many, the rest are dropped.
MAX_DEFERRED_CUSTOM_EVENTS = 1000


class PendingCustomEvents:
    """Custom events captured during a call. They go out when the call settles, with
    its own session and the identity resolved for it: a minted handle groups the
    call only once its result delivers it, and identify runs as the call records.

    Each event is fixed when captured: its top-level properties, its duration, and
    the context ``before_send`` runs in. Only the session is bound later."""

    def __init__(self) -> None:
        self.events: List[Tuple[Context, Dict[str, Any]]] = []
        self.settled: Optional[ResolvedSession] = None
        self.dropped = 0

    def defer(self, event_input: Dict[str, Any]) -> None:
        if len(self.events) >= MAX_DEFERRED_CUSTOM_EVENTS:
            self.dropped += 1
            if self.dropped == 1:
                log(
                    f"Warning: a tool call captured more than {MAX_DEFERRED_CUSTOM_EVENTS} "
                    "custom events; dropping the rest"
                )
            return
        properties = event_input.get("properties")
        snapshot = {
            **event_input,
            "properties": dict(properties)
            if isinstance(properties, Mapping)
            else properties,
            "duration": 0,
        }
        self.events.append((copy_context(), snapshot))


class _BoundSession(NamedTuple):
    data: MCPAnalyticsData
    session: ResolvedSession
    pending: Optional[PendingCustomEvents]


_in_tool_session: ContextVar[Optional[_BoundSession]] = ContextVar(
    "posthog_mcp_in_tool_session", default=None
)


@contextmanager
def bind_in_tool_session(
    data: MCPAnalyticsData,
    session: ResolvedSession,
    pending: Optional[PendingCustomEvents] = None,
) -> Iterator[None]:
    """Hold this call's session for the tool body. A concurrent request may rewrite
    ``data.session_id`` while the tool awaits, so a custom event captured inside
    the tool reads its session from here (see :func:`current_session`)."""
    token = _in_tool_session.set(_BoundSession(data, session, pending))
    try:
        yield
    finally:
        _in_tool_session.reset(token)


def current_session(data: MCPAnalyticsData) -> ResolvedSession:
    """The session a custom event belongs to: the running tool call's on this
    server, else the one the server last resolved."""
    bound = _in_tool_session.get()
    if bound is None or bound.data is not data:
        return ResolvedSession(data.session_id, data.session_source)
    if bound.pending is not None and bound.pending.settled is not None:
        return bound.pending.settled
    return bound.session


def unsettled_custom_events(data: MCPAnalyticsData) -> Optional[PendingCustomEvents]:
    """Where a custom event waits while the running call's session is undecided."""
    bound = _in_tool_session.get()
    if bound is None or bound.data is not data or bound.pending is None:
        return None
    return bound.pending if bound.pending.settled is None else None


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
    """Derive the SDK session id from a client conversation id.

    Keyed so two clients' ids, or a client id and a conversation handle, never
    share a hash input. Part of the cross-SDK contract, like
    :func:`derive_session_id_from_conversation`: posthog-js produces the same value
    byte for byte (``openai/session`` + ``v1/conv-example-0001`` is
    ``ses_2f5d35b7108d17c807766f2f769a1e8c``)."""
    return deterministic_prefixed_id("ses", f"{key}:{id}")


async def resolve_session_id(
    data: MCPAnalyticsData,
    mcp_session_id: Optional[str],
    *,
    token: Optional[SessionTokenPayload] = None,
    conversation_id: Optional[str] = None,
    client_conversation: Optional[ClientConversation] = None,
    request_session_id: Optional[str] = None,
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
        request_session_id=request_session_id,
    )
    return session_id


async def handshake_session(
    data: MCPAnalyticsData,
    mcp_session_id: Optional[str],
    token: Optional[SessionTokenPayload],
) -> ResolvedSession:
    """The session an ``initialize`` belongs to: the one the client's later requests
    resolve to through :func:`resolve_session_id_with_source`.

    A token or a transport session id gives it directly, so this leaves the server's
    shared session alone. One server serves many clients, and a handshake must not
    move the session that ``McpAnalytics.capture()`` reads. Without either, the
    session is this process's own, which only the resolver can hand out."""
    if token is not None:
        return ResolvedSession(token.session_id, "token")
    if mcp_session_id:
        return ResolvedSession(
            derive_session_id_from_mcp_session(mcp_session_id), "mcp"
        )
    return await resolve_session_id_with_source(data, None)


async def handshake_session_id(
    data: MCPAnalyticsData,
    mcp_session_id: Optional[str],
    token: Optional[SessionTokenPayload],
) -> str:
    """The id of :func:`handshake_session`, kept for callers of the earlier API."""
    return (await handshake_session(data, mcp_session_id, token)).session_id


def resolve_anchored_session(
    conversation_id: Optional[str],
    client_conversation: Optional[ClientConversation],
) -> Optional[ResolvedSession]:
    """Rules 1 and 2 of "Session identity": the session one call carries itself,
    which never touches the server's shared state."""
    if client_conversation is not None:
        return ResolvedSession(
            derive_session_id_from_client_conversation(*client_conversation),
            "client_metadata",
        )
    if conversation_id:
        return ResolvedSession(
            derive_session_id_from_conversation(conversation_id), "conversation"
        )
    return None


async def resolve_session_id_with_source(
    data: MCPAnalyticsData,
    mcp_session_id: Optional[str],
    *,
    token: Optional[SessionTokenPayload] = None,
    conversation_id: Optional[str] = None,
    client_conversation: Optional[ClientConversation] = None,
    request_session_id: Optional[str] = None,
) -> ResolvedSession:
    """Resolve the session id for a request. Mutates per-server state under a lock
    so concurrent async requests can't race on session rotation.

    Returns ``(session_id, source)`` where source is one of ``"conversation"``,
    ``"token"``, ``"mcp"``, ``"client_metadata"`` or ``"generated"`` -- describing *this* request. Callers
    must not infer it from ``data.session_source`` instead: that field is shared
    mutable state which the conversation branch below deliberately never writes, so
    reading it after the fact reports whatever some earlier request happened to
    leave there.

    Priority mirrors posthog-js ``resolveSession`` and the spec's "Session
    identity": a recognized client's own conversation id first (it sends it on
    every call), then the agent's ``conversation_id`` handle (the only id of ours
    that survives the 2026-07-28 revision's per-request server instances), then our
    self-encoded session token, then the transport's MCP session id, then this
    instance's own memory.

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

    ``request_session_id`` is a session the caller reserved for one HTTP request
    in place of process memory, which belongs to a one-client connection and on a
    shared HTTP server would group unrelated clients.
    """
    anchored = resolve_anchored_session(conversation_id, client_conversation)
    if anchored is not None:
        return anchored

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

        if request_session_id:
            # Stored only so an in-tool custom event lands in this request's
            # session. No later request reuses a "request" session.
            data.session_id = request_session_id
            data.session_source = "request"
            return ResolvedSession(request_session_id, "request")

        # Once a session is MCP-derived, keep it even if a later request arrives
        # without the MCP session id, so the session doesn't fragment.
        if data.session_source == "mcp" and data.last_mcp_session_id:
            data.last_activity = now
            return ResolvedSession(data.session_id, "mcp")

        # Memory fallback (single-owner transports like stdio). A leftover token
        # session must NOT leak to a credential-less request, so anything that
        # isn't already a generated session starts fresh; generated sessions
        # persist and roll over on inactivity.
        if data.session_source != "generated" or _is_stale(data, now):
            data.session_id = new_session_id()
            data.session_source = "generated"
        data.last_activity = now
        return ResolvedSession(data.session_id, "generated")


def _is_stale(data: MCPAnalyticsData, now: datetime) -> bool:
    timeout_seconds = INACTIVITY_TIMEOUT_IN_MINUTES * 60
    return (now - data.last_activity).total_seconds() > timeout_seconds


async def touch_connection_session(data: MCPAnalyticsData) -> None:
    """Count a finished call grouped by the client as connection activity, so a
    later call without an id does not rotate a live one-client connection's id.
    Rotation itself happens when a call arrives (see ``prime_session``).

    Only for a one-client connection (stdio): on a shared HTTP instance, keeping
    the instance's own session alive would group unrelated clients' requests."""
    async with data.session_lock:
        if data.session_source == "generated":
            data.last_activity = datetime.now(timezone.utc)
