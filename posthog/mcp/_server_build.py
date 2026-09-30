"""Validate server build identifiers for MCP analytics events."""

from __future__ import annotations

from typing import Any, Optional

MAX_SERVER_BUILD_LENGTH = 256


def validate_server_build(server_build: Any) -> Optional[str]:
    """Reject build identifiers that the event pipeline cannot record exactly."""
    if server_build is None:
        return None
    if not isinstance(server_build, str) or not server_build:
        raise TypeError("server_build must be a non-empty string.")
    if len(server_build) > MAX_SERVER_BUILD_LENGTH:
        raise ValueError(
            f"server_build must not exceed {MAX_SERVER_BUILD_LENGTH} characters."
        )
    return server_build
