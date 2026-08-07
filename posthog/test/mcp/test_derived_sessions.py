"""Tests for derived sessions: stateless MCP traffic (mcp 2.x / SEP-2567 has no
`Mcp-Session-Id` header) gets a stable `$session_id` derived from the identified
principal + client, so an identified user's calls correlate into one session
instead of fragmenting into one-per-request. Pure-posthog, runs in both envs."""

from datetime import datetime, timedelta, timezone

from parameterized import parameterized

from posthog.mcp import _derived_sessions as ds
from posthog.mcp._derived_sessions import (
    DerivedSessionRegistry,
    _MAX_ENTRIES,
)
from posthog.mcp.constants import INACTIVITY_TIMEOUT_IN_MINUTES


def _key(distinct_id="u1", client_name="cli", client_version="1.0"):
    return (distinct_id, client_name, client_version)


def test_same_key_within_gap_reuses_session():
    reg = DerivedSessionRegistry()
    now = datetime.now(timezone.utc)
    first = reg.resolve(*_key(), now=now)
    second = reg.resolve(*_key(), now=now + timedelta(minutes=1))
    assert first == second
    assert first.startswith("ses_")


def test_gap_expiry_rolls_session():
    reg = DerivedSessionRegistry()
    now = datetime.now(timezone.utc)
    first = reg.resolve(*_key(), now=now)
    later = now + timedelta(minutes=INACTIVITY_TIMEOUT_IN_MINUTES + 1)
    second = reg.resolve(*_key(), now=later)
    assert first != second


@parameterized.expand(
    [
        ("different_distinct_id", _key(distinct_id="u2")),
        ("different_client_name", _key(client_name="other")),
        ("different_client_version", _key(client_version="2.0")),
    ]
)
def test_different_key_gets_different_session(_name, other_key):
    reg = DerivedSessionRegistry()
    now = datetime.now(timezone.utc)
    base = reg.resolve(*_key(), now=now)
    other = reg.resolve(*other_key, now=now)
    assert base != other


def test_lru_bound_respected():
    reg = DerivedSessionRegistry()
    now = datetime.now(timezone.utc)
    for i in range(_MAX_ENTRIES + 100):
        reg.resolve(f"u{i}", "cli", "1.0", now=now)
    assert reg.size() <= _MAX_ENTRIES


def test_idle_entries_evicted_beyond_two_timeouts():
    reg = DerivedSessionRegistry()
    start = datetime.now(timezone.utc)
    reg.resolve("idle", "cli", "1.0", now=start)
    # A much later resolve for a different key triggers eviction of the idle one.
    far = start + timedelta(minutes=2 * INACTIVITY_TIMEOUT_IN_MINUTES + 1)
    reg.resolve("fresh", "cli", "1.0", now=far)
    assert reg.size() == 1


def test_concurrent_resolves_of_one_key_yield_one_session():
    import threading

    reg = DerivedSessionRegistry()
    now = datetime.now(timezone.utc)
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(reg.resolve(*_key(), now=now))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(results)) == 1


def test_fork_reset_clears_registry():
    ds._DERIVED_REGISTRY.resolve("u", "cli", "1.0")
    assert ds._DERIVED_REGISTRY.size() >= 1
    ds._reset_derived_registry_after_fork()
    assert ds._DERIVED_REGISTRY.size() == 0


@parameterized.expand(
    [
        # (has_token, mcp_session_id, has_identity) -> expected source, in precedence order.
        ("token_wins", True, "sess-1", True, "token"),
        ("mcp_over_derived", False, "sess-1", True, "mcp"),
        ("derived_when_identified", False, None, True, "derived"),
        ("generated_when_anonymous", False, None, False, "generated"),
    ]
)
async def test_resolve_session_id_precedence(
    _name, has_token, mcp_session_id, has_identity, expected_source
):
    from posthog.mcp._internal import MCPAnalyticsData
    from posthog.mcp.session import resolve_session_id
    from posthog.mcp.session_token import SessionTokenPayload
    from posthog.mcp.types import MCPAnalyticsOptions, UserIdentity

    data = MCPAnalyticsData(options=MCPAnalyticsOptions())
    token = (
        SessionTokenPayload(
            session_id="ses_from_token", client_name="c", client_version="1"
        )
        if has_token
        else None
    )
    identity = UserIdentity(distinct_id="u1") if has_identity else None

    await resolve_session_id(
        data,
        mcp_session_id,
        token=token,
        identity=identity,
        client_name="cli",
        client_version="1.0",
    )
    assert data.session_source == expected_source


async def test_derived_requires_distinct_id():
    from posthog.mcp._internal import MCPAnalyticsData
    from posthog.mcp.session import resolve_session_id
    from posthog.mcp.types import MCPAnalyticsOptions, UserIdentity

    data = MCPAnalyticsData(options=MCPAnalyticsOptions())
    # An identity with an empty distinct_id must NOT derive (would merge anon users).
    await resolve_session_id(
        data, None, identity=UserIdentity(distinct_id=""), client_name="cli"
    )
    assert data.session_source == "generated"
