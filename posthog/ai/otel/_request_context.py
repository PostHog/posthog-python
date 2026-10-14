"""Captures the active PostHog context for a span and applies it to an exported copy."""

import datetime
import decimal
import json
import logging
import math
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.util.types import AttributeValue

from ...contexts import get_context_distinct_id, get_context_session_id, get_tags

DISTINCT_ID_ATTRIBUTE = "posthog.distinct_id"
SESSION_ID_ATTRIBUTE = "$ai_session_id"
_PRIMITIVE_TYPES = (str, bool, int, float)

log = logging.getLogger("posthog")


@dataclass(frozen=True)
class RequestContext:
    distinct_id: Optional[str]
    session_id: Optional[str]
    tags: Dict[str, Any]


def capture_request_context() -> Optional[RequestContext]:
    """Snapshot the active context's distinct ID, session ID and tags, or None if all are empty."""
    snapshot = RequestContext(
        distinct_id=get_context_distinct_id() or None,
        session_id=get_context_session_id() or None,
        tags=dict(get_tags()),
    )
    if not (snapshot.distinct_id or snapshot.session_id or snapshot.tags):
        return None
    return snapshot


def request_context_attributes(context: RequestContext) -> Dict[str, AttributeValue]:
    """Return the snapshot as span attributes. Tags win over the context IDs."""
    values: Dict[str, Any] = {
        key: value
        for key, value in (
            (DISTINCT_ID_ATTRIBUTE, context.distinct_id),
            (SESSION_ID_ATTRIBUTE, context.session_id),
        )
        if value
    }
    values.update(context.tags)

    attributes: Dict[str, AttributeValue] = {}
    for key, value in values.items():
        try:
            attribute = to_attribute_value(value)
        except Exception:
            log.debug("Failed to read PostHog context value %r", key, exc_info=True)
            continue
        if attribute is not None:
            attributes[key] = attribute
    return attributes


def with_request_context(span: ReadableSpan, context: RequestContext) -> ReadableSpan:
    """Return a copy of ``span`` with the context's values added, keeping attributes it already has."""
    own = span.attributes or {}
    extra = {
        key: value
        for key, value in request_context_attributes(context).items()
        if key not in own
    }
    if not extra:
        return span
    return ReadableSpan(
        name=span.name,
        context=span.get_span_context(),
        parent=span.parent,
        resource=span.resource,
        attributes={**own, **extra},
        events=span.events,
        links=span.links,
        kind=span.kind,
        status=span.status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


def to_attribute_value(value: Any) -> Optional[AttributeValue]:
    """Return ``value`` as an OpenTelemetry attribute value, JSON-encoding anything OTel can't hold."""
    if value is None or (isinstance(value, str) and not value):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, _PRIMITIVE_TYPES):
        return value
    if isinstance(value, (list, tuple)) and _is_homogeneous(value):
        return list(value)
    try:
        encoded = json.dumps(value, allow_nan=False, default=_encode_string_like)
    except (TypeError, ValueError):
        return None
    if encoded.startswith('"'):
        return json.loads(encoded) or None
    return encoded


def _encode_string_like(value: Any) -> str:
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (uuid.UUID, decimal.Decimal)):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _is_homogeneous(values: Sequence[Any]) -> bool:
    return (
        all(isinstance(v, _PRIMITIVE_TYPES) for v in values)
        and all(math.isfinite(v) for v in values if isinstance(v, float))
        and len({type(v) for v in values}) <= 1
    )
