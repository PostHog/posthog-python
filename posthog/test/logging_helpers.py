import io
import logging
from contextlib import contextmanager
from typing import Iterator, Sequence

from posthog._logging import _POSTHOG_LOGGER_NAME


@contextmanager
def capture_message_only_logs(level: int = logging.DEBUG) -> Iterator[io.StringIO]:
    logger = logging.getLogger(_POSTHOG_LOGGER_NAME)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))

    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


def assert_before_send_failure_logged(
    records: Sequence[logging.LogRecord], event_name: str, error_type: str
) -> None:
    errors = [r for r in records if r.levelno >= logging.ERROR]
    assert [r.getMessage() for r in errors] == [
        f"[PostHog] Error in before_send callback for event {event_name} "
        f"({error_type}), dropping the event"
    ]
    assert errors[0].exc_info is None
    tracebacks = [r for r in records if r.exc_info]
    assert [(r.levelno, r.exc_info[0].__name__) for r in tracebacks] == [
        (logging.DEBUG, error_type)
    ]
