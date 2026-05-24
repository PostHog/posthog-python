import logging


log = logging.getLogger("posthog")
_fallback_warnings: set[tuple[str, str]] = set()


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


class OpenAIWrapperResource:
    def __init__(self, client, original):
        self._client = client
        self._original = original

    def __getattr__(self, name):
        """Fallback to original OpenAI object for any methods we don't explicitly handle."""
        warn_on_fallback(self.__class__.__name__, name)
        return getattr(self._original, name)
