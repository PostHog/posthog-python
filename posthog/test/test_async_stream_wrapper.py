"""Regression tests for AsyncStreamWrapper.

Ensures that PostHog AI streaming wrappers return objects that support both
the async iterator protocol (``async for``) and the async context manager
protocol (``async with``), as required by libraries such as pydantic-ai.

Issue: https://github.com/PostHog/posthog-python/issues/393
"""

import pytest

from posthog.ai.stream import AsyncStreamWrapper


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_gen(items):
    """Simple async generator that yields the given items."""
    for item in items:
        yield item


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_for_iteration():
    """AsyncStreamWrapper must yield all items when used with ``async for``."""
    wrapper = AsyncStreamWrapper(_make_gen([1, 2, 3]))
    result = []
    async for item in wrapper:
        result.append(item)
    assert result == [1, 2, 3]


@pytest.mark.asyncio
async def test_async_context_manager_protocol():
    """AsyncStreamWrapper must support ``async with`` without raising TypeError."""
    wrapper = AsyncStreamWrapper(_make_gen(["a", "b"]))

    # This is the call pattern that pydantic-ai uses and that previously raised:
    #   TypeError: 'async_generator' object does not support the asynchronous
    #   context manager protocol
    async with wrapper as stream:
        result = []
        async for chunk in stream:
            result.append(chunk)

    assert result == ["a", "b"]


@pytest.mark.asyncio
async def test_context_manager_returns_self():
    """``async with wrapper as w`` should bind the wrapper itself."""
    wrapper = AsyncStreamWrapper(_make_gen([]))
    async with wrapper as w:
        assert w is wrapper


@pytest.mark.asyncio
async def test_finally_block_runs_on_early_exit():
    """The underlying generator's finally block must run even when the caller
    breaks out of the loop early (i.e. doesn't fully exhaust the generator)."""
    finally_ran = []

    async def gen_with_finally():
        try:
            for i in range(10):
                yield i
        finally:
            finally_ran.append(True)

    wrapper = AsyncStreamWrapper(gen_with_finally())
    async with wrapper as stream:
        async for chunk in stream:
            if chunk == 2:
                break  # early exit

    # __aexit__ must have called aclose(), triggering the finally block
    assert finally_ran == [True], "finally block in generator did not run on early exit"


@pytest.mark.asyncio
async def test_finally_block_runs_on_full_exhaustion():
    """The underlying generator's finally block must also run on normal
    exhaustion (``aclose()`` on an exhausted generator is a no-op)."""
    finally_ran = []

    async def gen_with_finally():
        try:
            yield 1
            yield 2
        finally:
            finally_ran.append(True)

    wrapper = AsyncStreamWrapper(gen_with_finally())
    async with wrapper as stream:
        async for _ in stream:
            pass

    assert finally_ran == [True]


@pytest.mark.asyncio
async def test_attribute_proxy():
    """Attributes not on AsyncStreamWrapper itself should be forwarded to the
    underlying generator (for provider-specific metadata access)."""

    class FakeStream:
        extra_attr = "hello"

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self):
            pass

    wrapper = AsyncStreamWrapper(FakeStream())  # type: ignore[arg-type]
    assert wrapper.extra_attr == "hello"
