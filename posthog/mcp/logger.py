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

    Reserved for warnings that can only fire on an HTTP transport, where the
    STDIO constraint above does not apply. A default-configured host still sees
    these on stderr (logging's lastResort handler), which is the whole point:
    the misconfigurations this is used for are invisible in the data, so a
    warning nobody has opted in to receive is a warning nobody reads. Hosts that
    do configure logging can route or silence them by name like any other
    logger."""
    log(message)
    try:
        _stdlib_logger.warning(message)
    except Exception:
        pass
