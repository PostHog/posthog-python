"""Dropped-span accounting shared by span creation and export."""

import logging
import threading
import time
from typing import Dict

log = logging.getLogger("posthog")


class DropLog:
    """Counts dropped spans and warns at most once per interval, naming every reason.

    ``record()`` never logs: its callers hold their own locks, and a logging
    handler is application code. ``warn_if_due()`` does, with no lock held.
    """

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._lock = threading.Lock()
        self._count = 0
        # A dict for its order: reasons are named in the order they happened.
        self._reasons: Dict[str, None] = {}
        self._last_warning_at = 0.0

    def record(self, count: int, reason: str) -> None:
        with self._lock:
            self._count += count
            self._reasons[reason] = None

    def warn_if_due(self, force: bool = False) -> None:
        with self._lock:
            now = time.monotonic()
            if not self._count or (
                not force and now - self._last_warning_at < self._interval
            ):
                return
            message = "Dropping {} span(s): {}".format(
                self._count, "; ".join(self._reasons)
            )
            self._count = 0
            self._reasons.clear()
            self._last_warning_at = now
        log.warning(message)

    def reinit_after_fork(self) -> None:
        self._lock = threading.Lock()
        self._count = 0
        self._reasons.clear()
