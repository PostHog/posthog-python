"""Shared async streaming utilities for PostHog AI wrappers."""

from typing import Any, AsyncGenerator, Optional, TypeVar

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

    This class wraps the PostHog tracking generator and adds the missing
    ``__aenter__`` / ``__aexit__`` methods.  When available, it also keeps a
    reference to the original provider stream so that:

    - On ``__aexit__`` the tracking generator is closed (so the ``finally``
      block that fires the PostHog usage event always runs, even on early
      exit) **and** the underlying provider stream is closed (releasing the
      HTTP connection, matching the native SDK behaviour).
    - Attribute access not handled here is proxied to the provider stream, so
      provider-specific metadata such as ``.response`` keeps working.
    """

    def __init__(
        self,
        generator: AsyncGenerator[T, None],
        stream: Optional[Any] = None,
    ) -> None:
        self._generator = generator
        self._stream = stream

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
        # Close our tracking generator first so its `finally` block runs and
        # the PostHog usage event is captured, even when the caller breaks out
        # of the loop early. If it is already exhausted this is a no-op.
        await self._generator.aclose()

        # Then close the underlying provider stream to release the HTTP
        # connection, matching native SDK behaviour. Provider streams expose an
        # async `close()`; bare async generators (e.g. in tests) expose
        # `aclose()`.
        if self._stream is not None:
            close = getattr(self._stream, "aclose", None) or getattr(
                self._stream, "close", None
            )
            if close is not None:
                await close()

        return False

    # ------------------------------------------------------------------ #
    # Attribute proxy – forward any other attribute access to the         #
    # underlying provider stream (e.g. `.response`) when available.       #
    # ------------------------------------------------------------------ #

    def __getattr__(self, name: str) -> Any:
        target = self._stream if self._stream is not None else self._generator
        return getattr(target, name)
