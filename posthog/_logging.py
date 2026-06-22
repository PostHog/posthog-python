import logging


_POSTHOG_LOG_PREFIX = "[PostHog]"
_POSTHOG_LOGGER_NAME = "posthog"


class _PostHogLogPrefixFilter(logging.Filter):
    """Ensure PostHog SDK log messages are identifiable with message-only formatters."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_posthog_log_prefix_applied", False):
            return True

        message = record.getMessage()
        if not message.startswith(_POSTHOG_LOG_PREFIX):
            record.msg = f"{_POSTHOG_LOG_PREFIX} {message}"
            record.args = ()

        record._posthog_log_prefix_applied = True
        return True


def _configure_posthog_logging() -> None:
    logger = logging.getLogger(_POSTHOG_LOGGER_NAME)
    if not any(isinstance(f, _PostHogLogPrefixFilter) for f in logger.filters):
        logger.addFilter(_PostHogLogPrefixFilter())
