import logging
import math
import os
from typing import Any, Optional

__all__ = ["AI_TRACE_SAMPLE_RATE_ENV_VAR"]

AI_TRACE_SAMPLE_RATE_ENV_VAR = "POSTHOG_AI_TRACE_SAMPLE_RATE"

_FNV_OFFSET_BASIS_32 = 0x811C9DC5
_FNV_PRIME_32 = 0x01000193
_TWO_TO_32 = 4294967296  # 2 ** 32


def _fnv1a_32(text: str) -> int:
    h = _FNV_OFFSET_BASIS_32
    for b in text.encode("utf-8"):
        h ^= b
        h = (h * _FNV_PRIME_32) & 0xFFFFFFFF
    return h


def _fmix32(h: int) -> int:
    """Murmur3 32-bit finalizer to improve bit avalanche on sequential and shared-prefix trace IDs."""
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & 0xFFFFFFFF
    h ^= h >> 16
    return h


def _should_sample_ai_trace(trace_id: str, rate: float) -> bool:
    """Return True if the trace should be kept under the sample rate."""
    h = _fmix32(_fnv1a_32(trace_id))
    return (h / _TWO_TO_32) < rate


def _resolve_ai_trace_sample_rate(
    kwarg: Optional[float], log: logging.Logger
) -> Optional[float]:
    if kwarg is not None:
        raw_val = kwarg
        source = "ai_trace_sample_rate"
    else:
        env_val = os.environ.get(AI_TRACE_SAMPLE_RATE_ENV_VAR)
        if env_val is None or not env_val.strip():
            return None
        raw_val = env_val.strip()
        source = AI_TRACE_SAMPLE_RATE_ENV_VAR

    if isinstance(raw_val, bool):
        log.warning(
            "Invalid %s: %r. Expected a float between 0.0 and 1.0. Ignoring.",
            source,
            raw_val,
        )
        return None

    try:
        val = float(raw_val)
    except (ValueError, OverflowError):
        log.warning(
            "Invalid %s: %r. Expected a float between 0.0 and 1.0. Ignoring.",
            source,
            raw_val,
        )
        return None

    if not math.isfinite(val) or not (0.0 <= val <= 1.0):
        log.warning(
            "%s out of range [0.0, 1.0]: %r. Ignoring.",
            source,
            val,
        )
        return None

    return val


def _sample_ai_trace_event(
    msg: dict[str, Any], rate: Optional[float], log: logging.Logger
) -> bool:
    """Evaluate whether an event should be kept under ai_trace_sample_rate.

    Returns False if the event is an $ai_* event with a valid $ai_trace_id that is
    dropped by the hash-based sample rate. Returns True if the event should be kept.
    When kept, injects $ai_trace_sample_rate into msg["properties"].
    """
    if rate is None or rate >= 1.0:
        return True

    event_name = msg.get("event")
    if not (isinstance(event_name, str) and event_name.startswith("$ai_")):
        return True

    props = msg.get("properties")
    if not isinstance(props, dict):
        return True

    trace_id = props.get("$ai_trace_id")
    if trace_id is None:
        return True

    trace_id_str = str(trace_id)
    if not trace_id_str.strip():
        return True

    if not _should_sample_ai_trace(trace_id_str, rate):
        log.debug(
            "Event %s dropped by ai_trace_sample_rate (trace_id=%s)",
            event_name,
            trace_id_str,
        )
        return False

    props["$ai_trace_sample_rate"] = rate
    return True
