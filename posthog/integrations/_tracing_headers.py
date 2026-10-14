"""Shared request-scoped analytics attribution for framework integrations."""

import re
import warnings
from collections.abc import Callable
from typing import Optional

from .. import contexts

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MAX_HEADER_LENGTH = 1000


def _sanitize_tracing_header_value(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return _CONTROL_CHARS_RE.sub("", value).strip()[:_MAX_HEADER_LENGTH] or None


def _resolve_capture_tracing_headers(
    capture: Optional[bool],
    trust: Optional[bool],
    *,
    canonical_name: str = "capture_tracing_headers",
    deprecated_name: str = "trust_tracing_headers",
) -> bool:
    if trust is not None:
        warnings.warn(
            f"{deprecated_name} is deprecated; use {canonical_name} instead.",
            DeprecationWarning,
            stacklevel=3,
        )
    if capture is not None:
        return capture if isinstance(capture, bool) else False
    if trust is not None:
        return trust if isinstance(trust, bool) else False
    return True


def _read_tracing_header(
    get_header: Callable[[str], object], name: str
) -> Optional[str]:
    try:
        return _sanitize_tracing_header_value(get_header(name))
    except Exception:
        return None


def _apply_tracing_headers(
    get_header: Callable[[str], object],
    capture_tracing_headers: bool,
    fallback_distinct_id: Optional[str] = None,
) -> None:
    distinct_id = fallback_distinct_id
    if capture_tracing_headers:
        header_id = _read_tracing_header(get_header, "X-POSTHOG-DISTINCT-ID")
        session_id = _read_tracing_header(get_header, "X-POSTHOG-SESSION-ID")
        distinct_id = header_id or distinct_id
        if session_id:
            contexts.set_context_session(session_id)
    if distinct_id:
        contexts.identify_context(distinct_id)
