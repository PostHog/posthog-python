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


def _enrichment_is_enabled() -> bool:
    scope = contexts._get_current_context()
    while scope is not None:
        if scope.client is not None:
            return not scope.client.disabled
        scope = scope.parent

    from .. import api_key, default_client, disabled, project_api_key

    if disabled:
        return False
    if default_client is not None:
        return not default_client.disabled
    return bool((project_api_key or "").strip() or (api_key or "").strip())


def _enrich_span(span: object) -> None:
    try:
        if not _enrichment_is_enabled() or not getattr(span, "is_recording")():
            return
        attributes = getattr(span, "attributes", None)
        if not isinstance(attributes, Mapping):
            return
        context = contexts._get_active_span_context()
        extra = {}
        session_id = context["session_id"]
        distinct_id = context["distinct_id"]
        if session_id and "sessionId" not in attributes:
            extra["sessionId"] = session_id
        if distinct_id and not any(key in attributes for key in _IDENTITY_ATTRIBUTES):
            extra["posthogDistinctId"] = distinct_id
        if not extra:
            return
        # The SDK exposes no public capacity getter and evicts old attributes
        # when full. Skip enrichment if its private limit is unavailable.
        limit = getattr(getattr(span, "_limits", None), "max_span_attributes", None)
        if not isinstance(limit, int) or len(attributes) + len(extra) > limit:
            return
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
