"""Recover the exception behind a standalone FastMCP ``isError`` tool result.

FastMCP's ``tools/call`` handler, the one we wrap, turns whatever its public
``call_tool`` raised into ``CallToolResult(isError=True)``, keeping only the
message text. Wrapping ``call_tool`` on the server instance sees the exception
first, and hands it to the enclosing handler through a context variable.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Optional

_WRAPPED_FLAG = "__posthog_mcp_wrapped__"


@dataclass
class RaisedToolError:
    server: Any
    error: Optional[BaseException] = None


_current: ContextVar[Optional[RaisedToolError]] = ContextVar(
    "posthog_mcp_raised_tool_error", default=None
)


def wrap_call_tool(server: Any) -> None:
    original = server.call_tool
    if getattr(original, _WRAPPED_FLAG, False):
        return

    async def call_tool(*args: Any, **kwargs: Any) -> Any:
        # A tool calling another tool nests these calls. The outermost call
        # settles last, so the slot ends holding the outcome the client sees.
        try:
            result = await original(*args, **kwargs)
        except Exception as error:
            _settle(server, error)
            raise
        _settle(server, None)
        return result

    setattr(call_tool, _WRAPPED_FLAG, True)
    server.call_tool = call_tool


def _settle(server: Any, error: Optional[BaseException]) -> None:
    # Another server's call_tool, reached from inside a tool, must not write
    # its outcome onto this call.
    raised = _current.get()
    if raised is not None and raised.server is server:
        raised.error = error


@contextmanager
def raised_tool_error(server: Any) -> Iterator[RaisedToolError]:
    raised = RaisedToolError(server)
    token = _current.set(raised)
    try:
        yield raised
    finally:
        _current.reset(token)
