"""Shared async streaming utilities for PostHog AI wrappers."""

from typing import Any, AsyncGenerator, Generic, Optional, TypeVar

T = TypeVar("T")


class AsyncStreamWrapper(Generic[T]):
    """Adds the async context manager protocol to a PostHog streaming generator.

    The OpenAI and Anthropic SDK streams support both ``async for`` and
    ``async with``. PostHog's wrappers returned a bare async generator, which
    only supports ``async for``, so ``async with response:`` (used by
    pydantic-ai) raised a TypeError. This wraps the tracking generator and,
    when given the original provider stream, closes it and proxies attribute
    access (e.g. ``.response``) to it.
    """

    def __init__(
        self,
        generator: AsyncGenerator[T, None],
        stream: Optional[Any] = None,
    ) -> None:
        self._generator = generator
        self._stream = stream

    def __aiter__(self) -> "AsyncStreamWrapper[T]":
        return self

    async def __anext__(self) -> T:
        return await self._generator.__anext__()

    async def __aenter__(self) -> "AsyncStreamWrapper[T]":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        # Close the tracking generator first so its `finally` block captures the
        # PostHog event, even on early exit. try/finally guarantees the provider
        # stream is still closed if that capture raises.
        try:
            await self._generator.aclose()
        finally:
            if self._stream is not None:
                close = getattr(self._stream, "aclose", None) or getattr(
                    self._stream, "close", None
                )
                if close is not None:
                    await close()

        return False

    # Async-generator protocol methods belong to the tracking generator, not
    # the provider stream (provider AsyncStreams expose `close()`, not these).
    # Forwarding `aclose()` to the generator preserves the pre-wrapper behaviour
    # where `await response.aclose()` runs the generator's `finally` (firing the
    # PostHog event) instead of raising AttributeError.
    _GENERATOR_METHODS = ("aclose", "asend", "athrow")

    def __getattr__(self, name: str) -> Any:
        # Only proxy public attributes (e.g. `.response`). Private/dunder names
        # are not forwarded — this avoids infinite recursion if `_stream` isn't
        # set yet and stops `hasattr`/copy probes leaking to the provider stream.
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._GENERATOR_METHODS:
            return getattr(self._generator, name)
        target = self._stream if self._stream is not None else self._generator
        return getattr(target, name)
