"""Unit tests for AsyncStreamWrapper (no external SDKs required)."""

import pytest

from posthog.ai.stream import AsyncStreamWrapper


class RecordingStream:
    """Minimal async-iterable provider stream that records when it is closed."""

    def __init__(self, items):
        self._items = list(items)
        self.closed = False
        self.response = "provider-response"  # provider-specific metadata

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_async_for_iteration_still_works():
    async def gen():
        yield 1
        yield 2
        yield 3

    wrapper = AsyncStreamWrapper(gen())
    assert [item async for item in wrapper] == [1, 2, 3]


@pytest.mark.asyncio
async def test_async_with_yields_self_and_iterates():
    async def gen():
        yield "a"
        yield "b"

    wrapper = AsyncStreamWrapper(gen())
    async with wrapper as stream:
        assert stream is wrapper
        assert [item async for item in stream] == ["a", "b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("consume_all", [False, True])
async def test_finally_block_runs_on_exit(consume_all):
    """The generator's finally block must run on context exit, whether the
    caller exhausts the stream or breaks out of it early."""
    captured = []

    async def gen():
        try:
            yield 1
            yield 2
            yield 3
        finally:
            captured.append("done")

    async with AsyncStreamWrapper(gen()) as stream:
        async for item in stream:
            if not consume_all and item == 1:
                break

    assert captured == ["done"]


@pytest.mark.asyncio
async def test_exit_closes_underlying_provider_stream():
    source = RecordingStream([1, 2, 3])

    async def gen():
        async for item in source:
            yield item

    async with AsyncStreamWrapper(gen(), source) as stream:
        async for _ in stream:
            break  # early exit

    assert source.closed is True


@pytest.mark.asyncio
async def test_getattr_proxies_to_provider_stream():
    source = RecordingStream([])

    async def gen():
        if False:
            yield  # make this an async generator

    wrapper = AsyncStreamWrapper(gen(), source)
    # `.response` lives on the provider stream, not the generator.
    assert wrapper.response == "provider-response"
