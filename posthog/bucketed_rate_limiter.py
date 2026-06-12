# Python port of the posthog-js BucketedRateLimiter:
# https://github.com/PostHog/posthog-js/blob/main/packages/core/src/utils/bucketed-rate-limiter.ts
# Kept behaviorally identical so rate limiting is consistent across SDKs.

import logging
import threading
import time
from typing import Callable, Dict, Hashable, Optional, Union

ONE_DAY_IN_SECONDS = 86400.0

log = logging.getLogger("posthog")

Number = Union[int, float]


def _clamp_to_range(value, min_value: Number, max_value: Number, label: str) -> Number:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        log.warning(f"{label} must be a number. Using max value {max_value}.")
        return max_value
    if value > max_value:
        log.warning(f"{label} cannot be greater than {max_value}. Using {max_value}.")
        return max_value
    if value < min_value:
        log.warning(f"{label} cannot be less than {min_value}. Using {min_value}.")
        return min_value
    return value


class _Bucket:
    __slots__ = ("tokens", "last_access")

    def __init__(self, tokens: Number, last_access: float):
        self.tokens = tokens
        self.last_access = last_access


class BucketedRateLimiter:
    """Token bucket rate limiter that tracks a separate bucket per key.

    Each key starts with a full bucket of ``bucket_size`` tokens and every
    call to :meth:`consume_rate_limit` consumes one token. ``refill_rate``
    tokens are restored per elapsed ``refill_interval_seconds`` (whole
    intervals only, fractional elapsed time is carried over), capped at
    ``bucket_size``.

    Matching the posthog-js implementation, the call that empties a bucket is
    itself reported as rate limited — a burst over a fresh bucket lets
    ``bucket_size - 1`` events through before limiting kicks in — and
    ``on_bucket_rate_limited`` fires once each time a bucket is drained.

    Thread-safe. ``clock`` must return seconds and is injectable for tests.
    """

    def __init__(
        self,
        bucket_size: Number,
        refill_rate: Number,
        refill_interval_seconds: Number,
        on_bucket_rate_limited: Optional[Callable[[Hashable], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._bucket_size = _clamp_to_range(bucket_size, 0, 100, "bucket_size")
        self._refill_rate = _clamp_to_range(
            refill_rate, 0, self._bucket_size, "refill_rate"
        )
        self._refill_interval = _clamp_to_range(
            refill_interval_seconds, 0, ONE_DAY_IN_SECONDS, "refill_interval_seconds"
        )
        self._on_bucket_rate_limited = on_bucket_rate_limited
        self._clock = clock
        self._buckets: Dict[Hashable, _Bucket] = {}
        self._lock = threading.Lock()

    def _apply_refill(self, bucket: _Bucket, now: float) -> None:
        if self._refill_interval <= 0:
            bucket.tokens = self._bucket_size
            bucket.last_access = now
            return

        elapsed = now - bucket.last_access
        refill_intervals = int(elapsed // self._refill_interval)

        if refill_intervals > 0:
            tokens_to_add = refill_intervals * self._refill_rate
            bucket.tokens = min(bucket.tokens + tokens_to_add, self._bucket_size)
            # advance by whole intervals so fractional elapsed time still
            # counts towards the next refill
            bucket.last_access += refill_intervals * self._refill_interval

    def consume_rate_limit(self, key: Hashable) -> bool:
        """Consume one token for ``key``. Returns True if rate limited."""
        callback = None

        with self._lock:
            now = self._clock()
            bucket = self._buckets.get(key)

            if bucket is None:
                bucket = _Bucket(tokens=self._bucket_size, last_access=now)
                self._buckets[key] = bucket
            else:
                self._apply_refill(bucket, now)

            if bucket.tokens <= 0:
                return True

            bucket.tokens -= 1
            rate_limited = bucket.tokens <= 0
            if rate_limited:
                callback = self._on_bucket_rate_limited

        if callback is not None:
            callback(key)
        return rate_limited

    def stop(self) -> None:
        with self._lock:
            self._buckets.clear()
