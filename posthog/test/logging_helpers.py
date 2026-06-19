import io
import logging
from contextlib import contextmanager
from typing import Iterator


@contextmanager
def capture_message_only_logs(level: int = logging.DEBUG) -> Iterator[io.StringIO]:
    logger = logging.getLogger("posthog")
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
