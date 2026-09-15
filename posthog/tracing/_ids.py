"""W3C Trace Context identifiers: 16-byte trace ids and 8-byte span ids, lowercase hex.

The ingestion service zeroes an id of the wrong length rather than rejecting
it, which silently orphans the span, so every id is validated before it ships.
"""

import re
import secrets

TRACE_ID_BYTES = 16
SPAN_ID_BYTES = 8

TRACE_ID_HEX = TRACE_ID_BYTES * 2
SPAN_ID_HEX = SPAN_ID_BYTES * 2

INVALID_TRACE_ID = "0" * TRACE_ID_HEX
INVALID_SPAN_ID = "0" * SPAN_ID_HEX

_HEX_RE = re.compile(r"^[0-9a-f]+$")


def _random_hex_id(byte_length: int) -> str:
    hex_id = secrets.token_hex(byte_length)
    while hex_id == "0" * (byte_length * 2):
        hex_id = secrets.token_hex(byte_length)
    return hex_id


def new_trace_id() -> str:
    return _random_hex_id(TRACE_ID_BYTES)


def new_span_id() -> str:
    return _random_hex_id(SPAN_ID_BYTES)


def _is_valid_hex_id(value: object, length: int, invalid: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and value != invalid
        and _HEX_RE.fullmatch(value) is not None
    )


def is_valid_trace_id(value: object) -> bool:
    return _is_valid_hex_id(value, TRACE_ID_HEX, INVALID_TRACE_ID)


def is_valid_span_id(value: object) -> bool:
    return _is_valid_hex_id(value, SPAN_ID_HEX, INVALID_SPAN_ID)
