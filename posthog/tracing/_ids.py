"""W3C Trace Context identifiers: 16-byte trace ids and 8-byte span ids, lowercase hex.

The ingestion service zeroes an id of the wrong length rather than rejecting
it, which silently orphans the span. Ids are valid by construction: generated
here, or accepted from a header the parser matched in full.
"""

import secrets

TRACE_ID_BYTES = 16
SPAN_ID_BYTES = 8

TRACE_ID_HEX = TRACE_ID_BYTES * 2
SPAN_ID_HEX = SPAN_ID_BYTES * 2

INVALID_TRACE_ID = "0" * TRACE_ID_HEX
INVALID_SPAN_ID = "0" * SPAN_ID_HEX


def _random_hex_id(byte_length: int) -> str:
    hex_id = secrets.token_hex(byte_length)
    while hex_id == "0" * (byte_length * 2):
        hex_id = secrets.token_hex(byte_length)
    return hex_id


def new_trace_id() -> str:
    return _random_hex_id(TRACE_ID_BYTES)


def new_span_id() -> str:
    return _random_hex_id(SPAN_ID_BYTES)
