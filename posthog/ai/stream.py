"""Shared async streaming utilities for PostHog AI wrappers."""

from typing import Any, AsyncGenerator, TypeVar

T = TypeVar("T")


class AsyncStreamWrapper:
    """Wraps an async generator so it also implements the async context manager protocol.

    The OpenAI and Anthropic SDKs return stream objects that support both
    ``async for`` iteration **and** ``async with`` (i.e. they are both async
    iterators and async context managers).  PostHog's streaming wrappers
    previously returned a bare async generator, which only supports ``async
    for``.  Libraries such as pydantic-ai call ``async with response:`` before
    iterating, causing::

        TypeError: 'async_generator' object does not support the
        asynchronous context manager protocol

    This class wraps the underlying async generator and adds the missing
    ``__aenter__`` / ``__aexit__`` methods.  On ``__aexit__`` the generator is
    closed so that the ``finally`` block inside the generator (which fires the
    PostHog usage event) always executes, even when the caller breaks out of
    the loop early.
    """

    def __init__(self, generator: AsyncGenerator[T, None]) -> None:
        self._generator = generator

    # ------------------------------------------------------------------ #
    # Async iterator protocol                                              #
    # ------------------------------------------------------------------ #

    def __aiter__(self) -> "AsyncStreamWrapper":
        return self

    async def __anext__(self) -> T:
        return await self._generator.__anext__()

    # ------------------------------------------------------------------ #
    # Async context manager protocol                                       #
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> "AsyncStreamWrapper":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        # Close the generator so the finally block (PostHog event capture) runs
        # even on early exit.  If the generator is already exhausted this is a
        # no-op.
        await self._generator.aclose()
        return False

    # ------------------------------------------------------------------ #
    # Attribute proxy – forward any other attribute access to the         #
    # underlying generator (e.g. .response on an Anthropic stream).      #
    # ------------------------------------------------------------------ #

    def __getattr__(self, name: str) -> Any:
        return getattr(self._generator, name)
