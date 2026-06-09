import threading
import time
from typing import Callable


class ExceptionRateLimiter:
    """Fixed-window rate limiter used for exception capture.

    Behavior:
    - Counts events in a fixed time window (default 60s).
    - Allows up to ``max_exceptions`` events per window.
    - After the limit is reached, allows one event every ``post_limit_every``
      events to avoid completely starving signals in tight crash loops.

    The implementation is intentionally simple (O(1) memory) and thread-safe.

    Parameters
    - max_exceptions: non-negative int, number of allowed events per window.
    - window_seconds: positive float, window length in seconds.
    - post_limit_every: positive int, after the limit, allow 1 in ``post_limit_every``.
    - clock: callable returning a monotonic timestamp (in seconds). Useful for tests.
    """

    __slots__ = (
        "_max",
        "_window",
        "_count",
        "_window_start",
        "_lock",
        "_post_every",
        "_clock",
    )

    def __init__(
        self,
        max_exceptions: int = 100,
        window_seconds: float = 60.0,
        post_limit_every: int = 10,
        clock: Callable[[], float] = time.monotonic,
    ):
        if max_exceptions < 0:
            raise ValueError("max_exceptions must be >= 0")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        if post_limit_every <= 0:
            raise ValueError("post_limit_every must be > 0")

        self._max = int(max_exceptions)
        self._window = float(window_seconds)
        self._post_every = int(post_limit_every)
        self._count = 0
        self._clock = clock
        self._window_start = self._clock()
        self._lock = threading.Lock()

    def should_capture(self) -> bool:
        """Return True if the current event should be captured.

        This method is thread-safe.
        """
        with self._lock:
            now = self._clock()
            if now - self._window_start >= self._window:
                self._count = 0
                self._window_start = now

            self._count += 1

            if self._count <= self._max:
                return True

            # post-limit: capture every Nth event to keep occasional signal
            return self._count % self._post_every == 0
