"""Recover the exception behind an ``isError`` tool result.

Standalone FastMCP's ``tools/call`` handler, and the mcp 1.x low-level
``@server.call_tool()`` decorator, turn whatever the tool raised into
``CallToolResult(isError=True)``, keeping only the message text. A wrapper on
the server instance sees the exception first, and hands it to the enclosing
handler through a context variable.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Optional

_WRAPPED_FLAG = "__posthog_mcp_wrapped__"

# fastmcp 3+ dispatches tools/call through the public `call_tool`; 2.x has no
# `call_tool` and dispatches through `_call_tool_middleware`.
_DISPATCH_METHODS = ("call_tool", "_call_tool_middleware")


@dataclass
class RaisedToolError:
    error: Optional[BaseException] = None


@dataclass
class _Slot:
    server: Any
    error: Optional[BaseException] = None
    open: bool = True


_current: ContextVar[Optional[_Slot]] = ContextVar(
    "posthog_mcp_raised_tool_error", default=None
)


def wrap_tool_dispatch(server: Any) -> None:
    method = next((m for m in _DISPATCH_METHODS if hasattr(server, m)), None)
    if method is None:
        return
    original = getattr(server, method)
    if getattr(original, _WRAPPED_FLAG, False):
        return

    async def dispatch(*args: Any, **kwargs: Any) -> Any:
        # A tool calling another tool nests these calls. The outermost call
        # settles last, so the slot ends holding the outcome the client sees.
        try:
            result = await original(*args, **kwargs)
        except Exception as error:
            _settle(server, error)
            raise
        _settle(server, None)
        return result

    setattr(dispatch, _WRAPPED_FLAG, True)
    setattr(server, method, dispatch)


def wrap_error_result(server: Any) -> None:
    original = getattr(server, "_make_error_result", None)
    if original is None or getattr(original, _WRAPPED_FLAG, False):
        return

    def make_error_result(error_message: str) -> Any:
        # Input and output validation failures also land here from inside an
        # except block, but with a prefixed message. Only the tool's own raise
        # is passed through as its bare text.
        error = sys.exc_info()[1]
        if isinstance(error, Exception) and error_message == str(error):
            _settle(server, error)
        return original(error_message)

    setattr(make_error_result, _WRAPPED_FLAG, True)
    setattr(server, "_make_error_result", make_error_result)


def _settle(server: Any, error: Optional[BaseException]) -> None:
    # Another server's dispatch, reached from inside a tool, must not write
    # its outcome onto this call, nor may a background task that outlives it.
    slot = _current.get()
    if slot is not None and slot.open and slot.server is server:
        slot.error = error


@contextmanager
def raised_tool_error(server: Any) -> Iterator[RaisedToolError]:
    slot = _Slot(server)
    raised = RaisedToolError()
    token = _current.set(slot)
    try:
        yield raised
    finally:
        _current.reset(token)
        # A background task started by the tool keeps a copy of this context,
        # so the slot outlives the call; it must not keep the traceback alive.
        raised.error, slot.error = slot.error, None
        slot.open = False
