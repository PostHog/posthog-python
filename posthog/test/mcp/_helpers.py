"""Shared test helpers for the posthog.mcp suite.

Kept in one place (OnceAndOnlyOnce) so the fake client and the fire-and-forget
flush logic can't drift between the FastMCP, low-level, PostHogMCP, and M4 tests.
"""

import asyncio


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
    import posthog.mcp.instrumentation as instr

    for _ in range(10):
        await asyncio.sleep(0)
        pending = [t for t in list(instr._BACKGROUND_TASKS) if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.sleep(0)


def events_named(source, name):
    """Captured events with the given name. ``source`` may be a ``FakeClient``
    (reads ``.events``) or a raw list of event dicts (PostHogMCP tests)."""
    events = source.events if hasattr(source, "events") else source
    return [e for e in events if e["event"] == name]
