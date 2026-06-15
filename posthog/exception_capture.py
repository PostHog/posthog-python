# Portions of this file are derived from getsentry/sentry-python
# Copyright (c) 2018 Functional Software, Inc. dba Sentry
# Licensed under the MIT License: https://github.com/getsentry/sentry-python/blob/master/LICENSE

# 💖open source (under MIT License)

import logging
import sys
import threading
from typing import TYPE_CHECKING

from posthog.bucketed_rate_limiter import BucketedRateLimiter
from posthog.exception_utils import walk_exception_chain

if TYPE_CHECKING:
    from posthog.client import Client


class ExceptionCapture:
    log = logging.getLogger("posthog")

    # more generous defaults than the browser SDK (10, 1, 10) because one
    # server process aggregates exceptions across many users' requests
    DEFAULT_BUCKET_SIZE = 50
    DEFAULT_REFILL_RATE = 10
    DEFAULT_REFILL_INTERVAL_SECONDS = 10

    def __init__(
        self,
        client: "Client",
        rate_limiting_enabled=False,
        bucket_size=DEFAULT_BUCKET_SIZE,
        refill_rate=DEFAULT_REFILL_RATE,
        refill_interval_seconds=DEFAULT_REFILL_INTERVAL_SECONDS,
    ):
        self.client = client
        self.original_excepthook = sys.excepthook
        sys.excepthook = self.exception_handler
        threading.excepthook = self.thread_exception_handler
        # opt-in client-side rate limiting: per exception type, allow a burst
        # of captures, then refill over time
        self._rate_limiter = None
        if rate_limiting_enabled:
            self._rate_limiter = BucketedRateLimiter(
                bucket_size=bucket_size,
                refill_rate=refill_rate,
                refill_interval_seconds=refill_interval_seconds,
            )

    def close(self):
        sys.excepthook = self.original_excepthook
        if self._rate_limiter is not None:
            self._rate_limiter.stop()

    def exception_handler(self, exc_type, exc_value, exc_traceback):
        # don't affect default behaviour.
        self.capture_exception((exc_type, exc_value, exc_traceback))
        self.original_excepthook(exc_type, exc_value, exc_traceback)

    def thread_exception_handler(self, args):
        self.capture_exception((args.exc_type, args.exc_value, args.exc_traceback))

    def exception_receiver(self, exc_info, extra_properties):
        if "distinct_id" in extra_properties:
            metadata = {"distinct_id": extra_properties["distinct_id"]}
        else:
            metadata = None
        self.capture_exception((exc_info[0], exc_info[1], exc_info[2]), metadata)

    def capture_exception(self, exception, metadata=None):
        try:
            if self._rate_limiter is not None:
                exception_type = self._exception_type(exception)
                if self._rate_limiter.consume_rate_limit(exception_type):
                    self.log.info(
                        f"Skipping exception capture because of client rate limiting. exception={exception_type}"
                    )
                    return

            distinct_id = metadata.get("distinct_id") if metadata else None
            self.client.capture_exception(exception, distinct_id=distinct_id)
        except Exception as e:
            self.log.exception(f"Failed to capture exception: {e}")

    @staticmethod
    def _exception_type(exception):
        if isinstance(exception, tuple):
            exc_info = exception
        else:
            exc_info = (
                type(exception),
                exception,
                getattr(exception, "__traceback__", None),
            )

        # PostHog groups exceptions by the root cause of the chain:
        # exceptions_from_error_tuple reverses the walked chain, so
        # $exception_list[0].type is the deepest cause, not the wrapping
        # exception. Key on that same type so rate-limit buckets line up with
        # server-side grouping (e.g. `raise RuntimeError from ZeroDivisionError`
        # is keyed on ZeroDivisionError, not RuntimeError).
        exc_type = exc_info[0]
        for chained_type, _, _ in walk_exception_chain(exc_info):
            exc_type = chained_type

        return getattr(exc_type, "__name__", None) or "Exception"
