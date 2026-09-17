"""W3C Trace Context interop: ``traceparent`` and ``tracestate`` handling."""

import re
from dataclasses import dataclass
from typing import List, Optional

from ._ids import INVALID_SPAN_ID, INVALID_TRACE_ID

TRACE_FLAGS_SAMPLED = "01"
TRACE_FLAGS_UNSAMPLED = "00"

# A version above `00` may append fields after the first four.
_TRACEPARENT_RE = re.compile(
    r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})(-.*)?$"
)

# W3C bounds neither header; the caps keep a peer's header from being
# amplified onto every outbound call.
_TRACEPARENT_MAX_LENGTH = 512
_TRACESTATE_MAX_MEMBERS = 32
_TRACESTATE_MAX_LENGTH = 512
# W3C: when trimming, drop members longer than this first.
_TRACESTATE_LARGE_MEMBER_LENGTH = 128
_NON_PRINTABLE_ASCII_RE = re.compile(r"[^\x20-\x7e\t]")


@dataclass(frozen=True)
class RemoteSpanContext:
    trace_id: str
    span_id: str
    # `01` sampled or `00` sampled out; other flag bits are zeroed.
    flags: str


@dataclass(frozen=True)
class _TraceparentFields:
    version: str
    trace_id: str
    span_id: str
    flags: str


def _header_text(value: object) -> Optional[str]:
    """A header value as text; a raw ASGI scope carries it as ASCII bytes."""
    if isinstance(value, bytes):
        try:
            return value.decode("ascii")
        except UnicodeDecodeError:
            return None
    return value if isinstance(value, str) else None


def _match_traceparent(value: object) -> Optional[_TraceparentFields]:
    value = _header_text(value)
    if value is None:
        return None
    # Not lowercased: a conformant peer restarts the trace on uppercase hex.
    stripped = value.strip()
    if len(stripped) > _TRACEPARENT_MAX_LENGTH:
        return None
    match = _TRACEPARENT_RE.match(stripped)
    if match is None:
        return None
    version, trace_id, span_id, flags, trailing = match.group(1, 2, 3, 4, 5)
    if version == "ff" or (version == "00" and trailing):
        return None
    if trailing and _NON_PRINTABLE_ASCII_RE.search(trailing):
        return None
    if trace_id == INVALID_TRACE_ID or span_id == INVALID_SPAN_ID:
        return None
    return _TraceparentFields(version, trace_id, span_id, flags)


def parse_traceparent(value: object) -> Optional[RemoteSpanContext]:
    """Parse an inbound ``traceparent``; ``None`` for anything malformed.

    A sampled-out (``00``) trace is still continued, and the flag is carried
    onward so a downstream sampler sees the caller's decision.
    """
    fields = _match_traceparent(value)
    if fields is None:
        return None
    # W3C requires zeroing the flag bits version `00` does not define.
    flags = (
        TRACE_FLAGS_SAMPLED if int(fields.flags, 16) & 0x01 else TRACE_FLAGS_UNSAMPLED
    )
    return RemoteSpanContext(fields.trace_id, fields.span_id, flags)


def normalize_traceparent(value: object) -> Optional[str]:
    """The inbound ``traceparent`` as received, or ``None`` when it is malformed.

    Echoed whole rather than rebuilt, so a higher version keeps the fields this
    SDK does not read.
    """
    text = _header_text(value)
    if text is None or _match_traceparent(text) is None:
        return None
    return text.strip()


def traceparent_header(value: object) -> object:
    """Unwrap the one-element list a multi-value header API returns, and decode
    the bytes a raw ASGI scope carries.

    A longer list holds two different inbound values and is left to be rejected,
    as is a value that is not ASCII.
    """
    if isinstance(value, (list, tuple)) and len(value) == 1:
        value = value[0]
    if isinstance(value, bytes):
        return _header_text(value) or value
    return value


def format_traceparent(
    trace_id: str, span_id: str, flags: str = TRACE_FLAGS_SAMPLED
) -> str:
    return f"00-{trace_id}-{span_id}-{flags}"


def sanitize_tracestate(value: object) -> Optional[str]:
    """Validate an inbound ``tracestate``; ``None`` when it is malformed.

    An invalid value is discarded without invalidating its traceparent. One
    over 512 characters is trimmed by whole members.
    """
    text = _header_text(value)
    if text is None:
        return None
    trimmed = text.strip()
    if not trimmed or _NON_PRINTABLE_ASCII_RE.search(trimmed):
        return None
    members = trimmed.split(",")
    if len(members) > _TRACESTATE_MAX_MEMBERS:
        return None
    if any(member.strip() and "=" not in member for member in members):
        return None
    if len(trimmed) <= _TRACESTATE_MAX_LENGTH:
        return trimmed
    return _trim_to_length(members)


def _trim_to_length(members: List[str]) -> Optional[str]:
    """Drop large members first, then from the right, keeping those nearest the caller."""
    kept = list(members)

    def joined_length() -> int:
        return sum(len(member) for member in kept) + len(kept) - 1

    for index in range(len(kept) - 1, -1, -1):
        if joined_length() <= _TRACESTATE_MAX_LENGTH:
            break
        if len(kept[index]) > _TRACESTATE_LARGE_MEMBER_LENGTH:
            del kept[index]
    while kept and joined_length() > _TRACESTATE_MAX_LENGTH:
        kept.pop()
    return ",".join(kept) if kept else None
