# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Derived sessions for stateless MCP traffic.

The 2026-07-28 spec (SEP-2567) removed the ``Mcp-Session-Id`` header, so a
stateless / per-request server has no transport session to correlate a user's
tool calls into one ``$session_id``. SEP-2567's telemetry guidance is to derive
the session from "the authenticated principal ... or a request-level correlation
ID". This registry does exactly that: it maps
``(distinct_id, client_name, client_version)`` to a stable ``ses_`` id that rolls
over after the same inactivity timeout as the in-memory fallback.

The registry is module-level (not per-server) so that per-request server
instances created within one process share it — a stateless deployment spins up a
fresh server per request, and each would otherwise mint its own session. It never
derives without a ``distinct_id``: an anonymous key would merge unrelated users
under one session, so callers fall back to a generated session instead.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional

from .constants import INACTIVITY_TIMEOUT_IN_MINUTES
from ._ids import new_prefixed_id

# Bound the registry so a long-lived process serving many distinct users can't
# grow it without limit; the LRU evicts the least-recently-resolved key.
_MAX_ENTRIES = 10_000

# An entry idle beyond twice the inactivity timeout can never be reused (a reuse
# within one timeout is required, and past one timeout the session rolls anyway),
# so it is eligible for opportunistic eviction on the next resolve.
_IDLE_EVICTION_SECONDS = 2 * INACTIVITY_TIMEOUT_IN_MINUTES * 60

_DerivedKey = tuple[str, str, str]


class DerivedSessionRegistry:
    """Thread-safe, bounded map from an identity+client key to a rolling session
    id. Standalone (not tied to ``MCPAnalyticsData``) so a process's per-request
    server instances share one registry."""

    def __init__(self) -> None:
        # OrderedDict as an LRU: move_to_end on reuse, popitem(last=False) to evict.
        self._entries: "OrderedDict[_DerivedKey, tuple[str, datetime]]" = OrderedDict()
        self._lock = threading.Lock()

    def resolve(
        self,
        distinct_id: str,
        client_name: str,
        client_version: str,
        *,
        now: Optional[datetime] = None,
    ) -> str:
        """The stable session id for this key, minting a new one on first sight or
        after the inactivity timeout. ``now`` is injectable for deterministic tests."""
        now = now or datetime.now(timezone.utc)
        key = (distinct_id, client_name or "", client_version or "")
        timeout_seconds = INACTIVITY_TIMEOUT_IN_MINUTES * 60
        with self._lock:
            self._evict_idle_locked(now)
            existing = self._entries.get(key)
            if existing is not None:
                session_id, last_activity = existing
                if (now - last_activity).total_seconds() <= timeout_seconds:
                    self._entries[key] = (session_id, now)
                    self._entries.move_to_end(key)
                    return session_id
            session_id = new_prefixed_id("ses")
            self._entries[key] = (session_id, now)
            self._entries.move_to_end(key)
            self._enforce_bound_locked()
            return session_id

    def _evict_idle_locked(self, now: datetime) -> None:
        # Entries are time-ordered by last activity only loosely (LRU by access),
        # so scan for any idle beyond the eviction horizon. Bounded by _MAX_ENTRIES.
        stale = [
            key
            for key, (_sid, last_activity) in self._entries.items()
            if (now - last_activity).total_seconds() > _IDLE_EVICTION_SECONDS
        ]
        for key in stale:
            del self._entries[key]

    def _enforce_bound_locked(self) -> None:
        while len(self._entries) > _MAX_ENTRIES:
            self._entries.popitem(last=False)

    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_DERIVED_REGISTRY = DerivedSessionRegistry()


def derive_session_id(
    distinct_id: str,
    client_name: Optional[str],
    client_version: Optional[str],
    *,
    now: Optional[datetime] = None,
) -> str:
    """Resolve the process-shared derived session id for an identified request."""
    return _DERIVED_REGISTRY.resolve(
        distinct_id, client_name or "", client_version or "", now=now
    )


def _reset_derived_registry_after_fork() -> None:
    """Drop registry state inherited by a forked child.

    The lock may have been held by a thread that does not survive ``fork()``, so
    replace the whole registry rather than acquiring the inherited lock. Mirrors
    the background-loop fork reset in ``_instrumentation.py``."""
    global _DERIVED_REGISTRY
    _DERIVED_REGISTRY = DerivedSessionRegistry()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_derived_registry_after_fork)
