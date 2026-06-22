# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""STDIO-safe logger.

MCP servers running over the STDIO transport use stdout/stderr to exchange
protocol messages, so the SDK must never ``print``. We accept a ``logger``
option on the public API; when omitted, log calls are silently dropped. Plug in
any callable (e.g. a file logger, or ``print`` for non-STDIO transports).
"""

from __future__ import annotations

from typing import Callable, Optional

LoggerFn = Callable[[str], None]

_active_logger: Optional[LoggerFn] = None


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
