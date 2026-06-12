# Warn when a wrapper's base_url points at the PostHog AI Gateway: the gateway
# emits its own $ai_generation, so each call would be captured (and, for billable
# products, billed) twice. We only warn — the wrapper's event carries data the
# gateway never sees (groups, custom properties, trace hierarchy).

import logging
import re
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

log = logging.getLogger("posthog")

# Keep in sync with the gateway's deployed hosts (see services/llm-gateway in the
# main repo). gateway.us.posthog.com is live today; the rest are listed ahead of
# any traffic moving to them.
POSTHOG_AI_GATEWAY_HOSTS = [
    "gateway.posthog.com",
    "gateway.us.posthog.com",
    "gateway.eu.posthog.com",
    "ai-gateway.us.posthog.com",
    "ai-gateway.eu.posthog.com",
]

# Swap for the dedicated AI Gateway page once it ships.
_GATEWAY_DOCS_URL = "https://posthog.com/docs/ai-observability"

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)

# OTel spans don't pass through the capture funnels, so detect the gateway from
# the span's host/URL attributes instead. These follow the GenAI / HTTP semantic
# conventions: `server.address` is a bare host, `url.full` a full URL, both of
# which is_posthog_ai_gateway_url accepts.
_OTEL_GATEWAY_URL_ATTRIBUTES = ("server.address", "url.full")


def _extract_host(base_url: str) -> Optional[str]:
    try:
        # Tolerate bare hosts that omit a scheme, e.g. "gateway.us.posthog.com/v1".
        url = base_url if _SCHEME_RE.match(base_url) else f"https://{base_url}"
        host = urlparse(url).hostname
        return host.lower() if host else None
    except Exception:
        return None


def is_posthog_ai_gateway_url(base_url: Any) -> bool:
    """Return True if base_url points at a known PostHog AI Gateway host."""
    if not base_url:
        return False
    host = _extract_host(str(base_url))
    return host is not None and host in POSTHOG_AI_GATEWAY_HOSTS


def warn_if_posthog_ai_gateway(base_url: Any) -> None:
    """
    Warn when an AI wrapper is pointed at the PostHog AI Gateway.

    Warns on every gateway call by design: the misconfiguration is impossible to
    miss that way, and a doubled bill is worse than noisy logs. We only warn and
    never drop the event, because the wrapper event carries data the gateway
    never sees (groups, custom properties, trace hierarchy).
    """
    if not is_posthog_ai_gateway_url(base_url):
        return
    log.warning(
        "[PostHog] The PostHog AI wrapper is pointed at the PostHog AI Gateway. "
        "Both capture $ai_generation, so every call is double-counted and "
        "double-billed. Use one or the other — see %s.",
        _GATEWAY_DOCS_URL,
    )


def warn_if_posthog_ai_gateway_otel_attributes(
    attributes: Optional[Mapping[str, Any]],
) -> None:
    """Warn at most once per span when its host/URL attributes point at the gateway."""
    if not attributes:
        return
    for key in _OTEL_GATEWAY_URL_ATTRIBUTES:
        value = attributes.get(key)
        if isinstance(value, str) and is_posthog_ai_gateway_url(value):
            warn_if_posthog_ai_gateway(value)
            return
