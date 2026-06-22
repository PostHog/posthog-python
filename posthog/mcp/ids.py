# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""ID generation for MCP analytics.

``new_prefixed_id`` mints ``evt_<uuidv7>`` / ``ses_<uuidv7>`` ids.
``deterministic_prefixed_id`` maps an MCP protocol session id to a stable SDK
session id so the same MCP session reuses the same ``$session_id`` across server
restarts. UUIDv7 is implemented inline (RFC 9562) so we take on no extra
dependency; the FNV-1a hash is a faithful port of the TypeScript SDK.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Literal

MCPAnalyticsIDPrefix = Literal["evt", "ses"]


def _uuid7() -> str:
    """Generate a UUIDv7 (time-ordered) per RFC 9562, with no external dependency."""
    unix_ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = int.from_bytes(os.urandom(2), "big") & 0x0FFF  # 12 bits
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)  # 62 bits

    value = unix_ts_ms << 80
    value |= 0x7 << 76  # version 7
    value |= rand_a << 64
    value |= 0b10 << 62  # RFC 4122 variant
    value |= rand_b
    return str(uuid.UUID(int=value))


def new_prefixed_id(prefix: MCPAnalyticsIDPrefix) -> str:
    return f"{prefix}_{_uuid7()}"


def deterministic_prefixed_id(prefix: MCPAnalyticsIDPrefix, value: str) -> str:
    """Deterministic id derived from an arbitrary string.

    Uses the FNV-1a 64-bit hash (mixed twice to fill 32 hex chars). Not
    cryptographic; we only need a stable, low-collision input -> output mapping.
    """
    return f"{prefix}_{_fnv1a_hex(value)}{_fnv1a_hex(f'{value}::salt')}"


def _fnv1a_hex(value: str) -> str:
    # 64-bit FNV-1a implemented with two 32-bit halves, mirroring the TS SDK.
    h1 = 0x84222325
    h2 = 0xCBF29CE4
    for ch in value:
        c = ord(ch)
        h1 = ((h1 ^ c) * 0x000001B3) & 0xFFFFFFFF
        h2 = ((h2 ^ c) * 0x00000193) & 0xFFFFFFFF
    return f"{h1:08x}{h2:08x}"
