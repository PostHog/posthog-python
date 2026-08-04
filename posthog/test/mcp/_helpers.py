"""Shared test helpers for the posthog.mcp suite.

Kept in one place (OnceAndOnlyOnce) so the fake client and the fire-and-forget
flush logic can't drift between the FastMCP, low-level, PostHogMCP, and M4 tests.
"""

import asyncio
import concurrent.futures

import pytest

from posthog.mcp._mcp_version import installed_mcp_generation

# The 2026-07-28 SDK (`mcp` 2.x) is a breaking rewrite of the same PyPI package,
# so the two generations can't share a venv. The suite runs unchanged in both:
# tests that reach into a generation's private server internals (fastmcp import,
# `request_handlers` shape, session-token flows) carry the matching marker and
# skip cleanly in the other env. Prefer marking a whole module via
# `pytestmark = requires_mcp_v1` when every test in it is generation-specific.
_GENERATION = installed_mcp_generation()

requires_mcp_v1 = pytest.mark.skipif(
    _GENERATION != 1,
    reason=f"requires mcp 1.x internals; installed generation is {_GENERATION}",
)
requires_mcp_v2 = pytest.mark.skipif(
    _GENERATION != 2,
    reason=f"requires mcp 2.x internals; installed generation is {_GENERATION}",
)


class FakeClient:
    """Records capture() calls instead of sending them."""

    def __init__(self):
        self.events = []

    def capture(
        self,
        event,
        distinct_id=None,
        properties=None,
        timestamp=None,
        uuid=None,
        **kwargs,
    ):
        self.events.append(
            {"event": event, "distinct_id": distinct_id, "properties": properties or {}}
        )
        return None


async def flush_background():
    """Let fire-and-forget capture tasks run to completion."""
    import posthog.mcp._instrumentation as instr

    loop = asyncio.get_running_loop()
    for _ in range(10):
        await asyncio.sleep(0)
        pending = []
        for task in list(instr._BACKGROUND_TASKS):
            if isinstance(task, asyncio.Task) and task.get_loop() is loop:
                pending.append(task)
            elif isinstance(task, concurrent.futures.Future):
                pending.append(asyncio.wrap_future(task))
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.sleep(0)


def events_named(source, name):
    """Captured events with the given name. ``source`` may be a ``FakeClient``
    (reads ``.events``) or a raw list of event dicts (PostHogMCP tests)."""
    events = source.events if hasattr(source, "events") else source
    return [e for e in events if e["event"] == name]
