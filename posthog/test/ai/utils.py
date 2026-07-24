"""Shared test helpers for the AI wrapper test suites."""


def make_response_usage(
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
):
    """Build an ``openai.types.responses.ResponseUsage`` across SDK versions.

    openai has repeatedly added required fields to ``InputTokensDetails`` /
    ``OutputTokensDetails`` (e.g. ``cache_write_tokens`` in 2.45). Rather than
    hardcode a fixed field set that breaks on every such bump, this fills any
    required field it doesn't recognize with 0.
    """
    from openai.types.responses import ResponseUsage
    from openai.types.responses.response_usage import (
        InputTokensDetails,
        OutputTokensDetails,
    )

    def build(model_cls, known):
        values = dict(known)
        for name, field in model_cls.model_fields.items():
            if name not in values and field.is_required():
                values[name] = 0
        return model_cls(**values)

    return ResponseUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        input_tokens_details=build(
            InputTokensDetails, {"cached_tokens": cached_tokens}
        ),
        output_tokens_details=build(
            OutputTokensDetails, {"reasoning_tokens": reasoning_tokens}
        ),
    )


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
