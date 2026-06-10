import logging
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("posthog")

# Hosts that resolve to the PostHog AI Gateway. Host-only (no scheme/path).
# Detection is host-based, so the scheme, port, and route prefix don't matter.
POSTHOG_AI_GATEWAY_HOSTS = [
    "gateway.us.posthog.com",
]


def is_posthog_ai_gateway_url(base_url: Any) -> bool:
    """Return True if base_url points at a known PostHog AI Gateway host."""
    if not base_url:
        return False
    try:
        host = urlparse(str(base_url)).hostname
    except Exception:
        return False
    return host in POSTHOG_AI_GATEWAY_HOSTS


def warn_if_posthog_ai_gateway(base_url: Any) -> None:
    """
    Warn when an AI wrapper is pointed at the PostHog AI Gateway.

    The wrapper and the gateway each capture the LLM generation, which would
    double-count (and double-bill) the event. We only warn and never drop the
    event, because the wrapper event carries data the gateway never sees
    (groups, custom properties, trace hierarchy). We warn on every call rather
    than once, since a single startup line is easy to miss.
    """
    if is_posthog_ai_gateway_url(base_url):
        log.warning(
            "Your PostHog AI wrapper is pointed at the PostHog AI Gateway (%s). "
            "This will capture and bill each LLM generation twice — once by this "
            "wrapper and once by the gateway. Point the wrapper at the model "
            "provider's API directly, or remove the wrapper and rely on the "
            "gateway. See https://posthog.com/docs/ai-observability",
            urlparse(str(base_url)).hostname,
        )
