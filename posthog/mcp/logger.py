# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""STDIO-safe logger.

MCP servers running over the STDIO transport use stdout/stderr to exchange
protocol messages, so the SDK must never ``print``. We accept a ``logger``
option on the public API; when omitted, log calls are silently dropped. Plug in
any callable (e.g. a file logger, or ``print`` for non-STDIO transports).

:func:`warn` is the exception to "silently dropped" -- see its docstring.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

__all__ = ["set_logger"]

LoggerFn = Callable[[str], None]

_active_logger: Optional[LoggerFn] = None

_stdlib_logger = logging.getLogger("posthog.mcp")


def set_logger(logger: Optional[LoggerFn]) -> None:
    global _active_logger
    _active_logger = logger


def log(message: str) -> None:
    if _active_logger is not None:
        try:
            _active_logger(message)
        except Exception:
            # never let logging blow up the tracking pipeline
            pass


def warn(message: str) -> None:
    """A misconfiguration the host almost certainly wants to know about, sent to
    the ``logger`` option *and* to the ``posthog.mcp`` standard-library logger.

    Reserved for misconfigurations that are invisible in the captured data --
    nothing errors, the numbers just quietly stop meaning what the host thinks
    they mean. A warning nobody has opted in to receive is a warning nobody
    reads, so these go out whether or not a ``logger`` option was passed: a
    default-configured host sees them on stderr (logging's lastResort handler),
    and hosts that do configure logging can route or silence them by name like
    any other logger.

    Still STDIO-safe: the constraint above is on *stdout*, which carries the
    protocol stream, and the MCP spec explicitly allows servers to log to
    stderr."""
    log(message)
    try:
        _stdlib_logger.warning(message)
    except Exception:
        pass
