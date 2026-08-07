"""Shared async streaming utilities for PostHog AI wrappers."""

from typing import Any, AsyncGenerator, Generator, Generic, Optional, TypeVar

T = TypeVar("T")


class _StreamWrapper(Generic[T]):
    """Preserves a provider stream's helpers while tracking its iteration."""

    def __init__(
        self,
        generator: Generator[T, None, None],
        stream: Any,
    ) -> None:
        self._generator = generator
        self._stream = stream

    def __iter__(self) -> "_StreamWrapper[T]":
        return self

    def __next__(self) -> T:
        return next(self._generator)

    def __enter__(self) -> "_StreamWrapper[T]":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._generator.close()
        finally:
            self._stream.close()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in ("send", "throw"):
            return getattr(self._generator, name)
        return getattr(self._stream, name)


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
        await self.close()
        return False

    async def close(self) -> None:
        # Close the generator first so its `finally` captures the event, even on
        # early exit. try/finally still closes the provider stream if that raises.
        try:
            await self._generator.aclose()
        finally:
            if self._stream is not None:
                close = getattr(self._stream, "aclose", None) or getattr(
                    self._stream, "close", None
                )
                if close is not None:
                    await close()

    async def aclose(self) -> None:
        await self.close()

    _GENERATOR_METHODS = ("asend", "athrow")

    def __getattr__(self, name: str) -> Any:
        # Proxy only public attributes (e.g. `.response`) to the provider stream.
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._GENERATOR_METHODS:
            return getattr(self._generator, name)
        target = self._stream if self._stream is not None else self._generator
        return getattr(target, name)
