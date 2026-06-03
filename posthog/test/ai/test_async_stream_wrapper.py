"""Unit tests for AsyncStreamWrapper (no external SDKs required)."""

import pytest

from posthog.ai.stream import AsyncStreamWrapper
from posthog.test.ai.utils import RecordingAsyncStream


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
    source = RecordingAsyncStream([1, 2, 3])

    async def gen():
        async for item in source:
            yield item

    async with AsyncStreamWrapper(gen(), source) as stream:
        async for _ in stream:
            break

    assert source.closed is True


@pytest.mark.asyncio
async def test_provider_stream_closed_even_if_generator_aclose_raises():
    """The try/finally in __aexit__ must still close the provider stream when
    the generator's finally (the PostHog capture) raises."""
    source = RecordingAsyncStream([1, 2, 3])

    async def gen():
        try:
            async for item in source:
                yield item
        finally:
            raise RuntimeError("capture blew up")

    with pytest.raises(RuntimeError, match="capture blew up"):
        async with AsyncStreamWrapper(gen(), source) as stream:
            async for _ in stream:
                break

    assert source.closed is True


@pytest.mark.asyncio
async def test_exception_in_body_propagates():
    """__aexit__ returns False, so exceptions in the body must propagate (not be
    swallowed), and the provider stream is still closed on the error path."""
    source = RecordingAsyncStream([1, 2, 3])

    async def gen():
        async for item in source:
            yield item

    with pytest.raises(ValueError, match="boom"):
        async with AsyncStreamWrapper(gen(), source) as stream:
            async for _ in stream:
                raise ValueError("boom")

    assert source.closed is True


@pytest.mark.asyncio
async def test_getattr_proxies_to_provider_stream():
    source = RecordingAsyncStream([])

    async def gen():
        if False:
            yield  # make this an async generator

    wrapper = AsyncStreamWrapper(gen(), source)
    assert wrapper.response == "provider-response"


@pytest.mark.asyncio
async def test_aclose_runs_generator_finally_and_captures():
    """`await response.aclose()` must close the tracking generator (firing its
    finally) rather than proxying to the provider stream, which has no aclose."""
    source = RecordingAsyncStream([1, 2, 3])
    captured = []

    async def gen():
        try:
            async for item in source:
                yield item
        finally:
            captured.append("done")

    wrapper = AsyncStreamWrapper(gen(), source)
    await wrapper.__anext__()
    await wrapper.aclose()

    assert captured == ["done"]


@pytest.mark.asyncio
async def test_getattr_does_not_proxy_private_names():
    source = RecordingAsyncStream([])

    async def gen():
        if False:
            yield

    wrapper = AsyncStreamWrapper(gen(), source)
    assert not hasattr(wrapper, "_nonexistent_private")
