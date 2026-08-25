import logging
from typing import Any, Dict, Mapping, Optional


log = logging.getLogger("posthog")
_fallback_warnings: set[tuple[str, str]] = set()


def merge_provider_override(
    posthog_properties: Optional[Dict[str, Any]],
    posthog_provider_override: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Fold ``posthog_provider_override`` into ``posthog_properties``.

    The wrapper always drives OpenAI's own response-format parsing with the
    literal provider name "openai" -- that must stay untouched even when
    talking to an OpenAI-compatible endpoint (DeepSeek, Groq, etc. via a
    custom ``base_url``), since those responses are still shaped like
    OpenAI's. What callers want to override is only the ``$ai_provider``
    value reported on the captured event, so PostHog's cost attribution
    matches the real provider.

    Every capture path merges ``posthog_properties`` into the event's
    properties *after* the default ``$ai_provider`` value, so setting the
    key here is sufficient to win without touching format selection.

    Returns ``posthog_properties`` unchanged when no override is supplied,
    so omitting the parameter leaves behavior identical to before it
    existed.
    """
    if posthog_provider_override is None:
        return posthog_properties
    return {**(posthog_properties or {}), "$ai_provider": posthog_provider_override}


def _wrap_openai_resources(client: Any, wrappers: Mapping[str, type]) -> None:
    """Save and replace available SDK resources using an explicit wrapper mapping."""
    for resource_name, wrapper_type in wrappers.items():
        original = getattr(client, resource_name, None)
        setattr(client, f"_original_{resource_name}", original)
        if original is not None:
            setattr(client, resource_name, wrapper_type(client, original))


def reset_fallback_warnings() -> None:
    _fallback_warnings.clear()


def warn_on_fallback(wrapper_name: str, name: str) -> None:
    key = (wrapper_name, name)
    if key in _fallback_warnings:
        return

    _fallback_warnings.add(key)
    log.warning(
        "Falling back to unwrapped OpenAI API for %s.%s; PostHog LLM tracking "
        "and posthog_* arguments will not be applied.",
        wrapper_name,
        name,
    )


class _OpenAIWrapperResource:
    def __init__(self, client, original):
        self._client = client
        self._original = original

    def __getattr__(self, name):
        """Fallback to original OpenAI object for any methods we don't explicitly handle."""
        warn_on_fallback(self.__class__.__name__, name)
        return getattr(self._original, name)
