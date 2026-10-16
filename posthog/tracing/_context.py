import logging
from collections.abc import Mapping

from .. import contexts

log = logging.getLogger("posthog")
_IDENTITY_ATTRIBUTES = (
    "posthogDistinctId",
    "posthog.distinct_id",
    "user.id",
    "enduser.id",
)


def _enrich_span(span: object) -> None:
    try:
        if not getattr(span, "is_recording")():
            return
        attributes = getattr(span, "attributes", None)
        if not isinstance(attributes, Mapping):
            return
        context = contexts._get_active_span_context()
        if context is None:
            return
        extra = {}
        session_id = context.get_session_id()
        distinct_id = context.get_distinct_id()
        if session_id and "sessionId" not in attributes:
            extra["sessionId"] = session_id
        if distinct_id and not any(key in attributes for key in _IDENTITY_ATTRIBUTES):
            extra["posthogDistinctId"] = distinct_id
        if extra:
            getattr(span, "set_attributes")(extra)
    except Exception:
        log.debug("Failed to add PostHog context to span", exc_info=True)


def _enrich_current_span() -> None:
    try:
        from opentelemetry.trace import get_current_span

        _enrich_span(get_current_span())
    except ImportError:
        pass
    except Exception:
        log.debug("Failed to read current OpenTelemetry span", exc_info=True)
