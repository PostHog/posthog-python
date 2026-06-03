"""Shared test helpers for the AI wrapper test suites."""


class RecordingAsyncStream:
    """Mock provider async stream that is iterable and records when closed.

    Mirrors the real ``openai.AsyncStream`` / ``anthropic.AsyncStream``: it
    supports ``async for`` and exposes an async ``close()`` plus a ``response``
    attribute, so tests can assert both iteration and that the underlying
    stream is closed on context exit.
    """

    def __init__(self, items):
        self._items = list(items)
        self.closed = False
        self.response = "provider-response"

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)

    async def close(self):
        self.closed = True
