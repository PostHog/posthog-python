"""Client-side validity.

The ingestion service rejects the whole request when one span is malformed (a
timestamp outside signed 64-bit nanoseconds, say), so every span is sanitized
before it is queued.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Union

log = logging.getLogger("posthog")

FALLBACK_SPAN_NAME = "unknown"

# OTLP declares the timestamps fixed64, but the service parses signed 64-bit.
MAX_TIMESTAMP_NS = 2**63 - 1
MIN_TIMESTAMP_NS = 0

# The server clamps timestamps more than 24 hours from receive time.
DEEP_BACKDATE_WARNING_NS = 24 * 60 * 60 * 10**9

UNSERIALIZABLE_VALUE = "[Unserializable]"
FUNCTION_VALUE = "[Function]"

SpanTimeInput = Union[datetime, int, float]

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def sanitize_name(name: Any, label: str, max_length: Optional[int] = None) -> str:
    """A non-empty name, truncated to ``max_length``; an unusable one becomes ``unknown``."""
    if isinstance(name, str) and name.strip():
        return name if max_length is None else name[:max_length]
    log.debug('%s must be a non-empty string; using "%s"', label, FALLBACK_SPAN_NAME)
    return FALLBACK_SPAN_NAME


def safe_str(value: Any) -> str:
    """``str(value)``, or a marker when ``__str__`` raises."""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return UNSERIALIZABLE_VALUE


def attribute_key(key: Any) -> Optional[str]:
    """An attribute key as a string, or ``None`` when ``str()`` raises."""
    if isinstance(key, str):
        return key
    try:
        return str(key)
    except Exception:
        log.debug("Dropping an attribute whose key cannot be converted to a string")
        return None


def to_epoch_ns(value: Any) -> Optional[int]:
    """A ``datetime`` or epoch seconds as unix nanoseconds; ``None`` when unusable."""
    if value is None or isinstance(value, bool):
        return None
    ns: int
    if isinstance(value, datetime):
        try:
            if value.tzinfo is None:
                value = value.astimezone()
            delta = value - _EPOCH
            ns = (
                delta.days * 86400 + delta.seconds
            ) * 10**9 + delta.microseconds * 1000
        except Exception:
            return None
    elif isinstance(value, int):
        ns = value * 10**9
    elif isinstance(value, float):
        scaled = value * 1e9
        if not math.isfinite(scaled):
            return None
        ns = int(round(scaled))
    else:
        return None
    if ns < MIN_TIMESTAMP_NS or ns > MAX_TIMESTAMP_NS:
        return None
    return ns


def resolve_start_ns(value: Any, now_ns: int) -> int:
    """A caller-supplied start time, or now; warns when the server will clamp it."""
    supplied = to_epoch_ns(value)
    if supplied is None:
        if value is not None:
            log.debug(
                "Span start_time is out of range or not a valid time; using the current time"
            )
        return now_ns
    if now_ns - supplied > DEEP_BACKDATE_WARNING_NS:
        log.debug(
            "Span start_time is more than 24 hours in the past; the server will clamp it "
            "to receive time and keep the original in $originalTimestamp"
        )
    elif supplied > now_ns:
        log.debug(
            "Span start_time is in the future; the span may export with a zero duration"
        )
    return supplied


def clamp_end_ns(end_ns: int, start_ns: int) -> int:
    return start_ns if end_ns < start_ns else end_ns


def resolve_supplied_ns(value: Any, derived_ns: int, label: str) -> int:
    """A caller-supplied end or event time, or the span's own clock when unusable."""
    supplied = to_epoch_ns(value)
    if supplied is None:
        if value is not None:
            log.debug(
                "Span %s is out of range or not a valid time; using the derived time",
                label,
            )
        return derived_ns
    return supplied


def copy_user_attributes(target: dict, source: Any) -> dict:
    """Copy caller-supplied attributes onto ``target`` key by key.

    A raising accessor costs its own key rather than the whole span.
    """
    if source is None:
        return target
    if not isinstance(source, Mapping):
        log.debug("Ignoring span attributes: expected a mapping, got %s", type(source))
        return target
    try:
        keys = list(source.keys())
    except Exception:
        return target
    for key in keys:
        key_str = attribute_key(key)
        if key_str is None:
            continue
        try:
            value = source[key]
        except Exception:
            value = UNSERIALIZABLE_VALUE
        target[key_str] = value
    return target
